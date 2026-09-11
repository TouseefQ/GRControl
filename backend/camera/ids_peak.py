"""
IDS Peak SDK wrapper for the IDS U3-34L0XCP camera.
Runs blocking SDK calls in a thread pool to keep the async event loop free.
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
import json

log = logging.getLogger(__name__)
# IMPORTANT: single worker. Every IDS peak SDK call — Library.Initialize(), open,
# get_info, set_exposure/gain, preview grab, capture, close — must run on the SAME
# thread. The library's initialization is bound to the thread that called
# Initialize(); a call dispatched to any other worker fails with
# PEAK_RETURN_CODE_NOT_INITIALIZED (black preview). A single worker also serializes
# access to the (non-thread-safe) node map and data stream, so a settings change
# can't race the live-preview grab. Do not raise max_workers.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ids-cam")


def _run_sync(fn, *args):
    """Run a synchronous function in the thread pool."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, fn, *args)


class IDSCamera:
    """
    Wraps the IDS Peak Python SDK.
    Import of ids_peak / ids_peak_ipl is deferred so the backend starts
    even when the SDK is not installed (useful during development).
    """

    def __init__(self, settings=None):
        from ..models import Settings
        self._settings = settings or Settings()
        self._device = None
        self._data_stream = None
        self._node_map = None
        self._open = False
        self._ids_peak = None
        self._ids_ipl = None
        self._ids_ipl_ext = None
        self._flip_x = False
        self._flip_y = False
        self._conv_mode = None       # cached Bayer→BGRa8 conversion mode
        self._wb_software = False     # software gray-world WB fallback active?
        self._pixel_format = None     # active PixelFormat symbolic name (raw depth)
        self._num_buffers = 16        # buffer count announced at open (see _open_first_camera)
        self._in_format_switch = False  # True while retuning format for a capture (gates _recover)
        # USB-stall auto-recovery: rate-limit restart attempts so a 15 fps preview
        # loop can't hammer a wedged camera (see _recover).
        self._last_recover_ts = 0.0
        self._recover_cooldown_s = 3.0    # min gap between recovery attempts
        self._recover_escalate_s = 10.0   # a re-stall within this → skip to reopen
        self._last_recover_was_restart = False

    @staticmethod
    def _ensure_gentl_path():
        """The IDS peak GenTL producers (.cti) are located via the
        GENICAM_GENTL64_PATH environment variable. The pip wheels do not
        bundle them, so if the variable is unset, point it at the producers
        from a system IDS peak install."""
        if os.environ.get("GENICAM_GENTL64_PATH"):
            return
        candidates = [
            "/usr/local/lib/x86_64-linux-gnu/ids-peak/cti",
            "/usr/lib/x86_64-linux-gnu/ids-peak/cti",
            "/opt/ids-peak/lib/ids-peak/cti",
        ]
        for d in candidates:
            if any(Path(d).glob("*.cti")) if Path(d).is_dir() else False:
                os.environ["GENICAM_GENTL64_PATH"] = d
                log.info("Set GENICAM_GENTL64_PATH=%s", d)
                return
        log.warning("No IDS GenTL producers (.cti) found — set GENICAM_GENTL64_PATH manually")

    def _load_sdk(self):
        self._ensure_gentl_path()
        try:
            # The IDS peak Python API exposes its symbols on submodules of the
            # same name (e.g. ids_peak.Library lives in ids_peak.ids_peak),
            # so the binding must be imported as `from <pkg> import <pkg>`.
            # ids_peak_ipl_extension provides BufferToImage, which wraps a raw
            # acquisition buffer as an IPL image we can convert and save.
            from ids_peak import ids_peak
            from ids_peak_ipl import ids_peak_ipl
            from ids_peak import ids_peak_ipl_extension
            self._ids_peak = ids_peak
            self._ids_ipl = ids_peak_ipl
            self._ids_ipl_ext = ids_peak_ipl_extension
            return True
        except ImportError:
            log.warning("ids_peak SDK not found — camera features disabled")
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _buffer_to_color_image(self, raw_buffer):
        """Wrap a finished acquisition buffer as an IPL image, convert it to
        BGRa8 (a new image with its own memory), then requeue the buffer."""
        ipl = self._ids_ipl
        image = self._ids_ipl_ext.BufferToImage(raw_buffer)
        # HighQuality gives the best colour from the Bayer pattern; this SDK
        # build has no ConversionMode_HQ, so prefer HighQuality then Fast.
        # Resolved once at open and cached (this runs on every frame).
        if self._conv_mode is None:
            self._conv_mode = getattr(ipl, "ConversionMode_HighQuality",
                                      ipl.ConversionMode_Fast)
        converted = image.ConvertTo(ipl.PixelFormatName_BGRa8, self._conv_mode)
        self._data_stream.QueueBuffer(raw_buffer)
        return converted

    @staticmethod
    def _ipl_to_numpy(image):
        """Return the IPL image pixels as a NumPy array, tolerant of the exact
        accessor name across SDK versions (get_numpy_3D / get_numpy / ...)."""
        for name in ("get_numpy_3D", "get_numpy_2D", "get_numpy", "get_numpy_1D"):
            fn = getattr(image, name, None)
            if fn is not None:
                return fn()
        raise RuntimeError("IPL image has no known NumPy accessor")

    @staticmethod
    def _apply_gray_world_wb(arr):
        """Gray-world white balance on an RGB(A) uint8 array: scale each colour
        channel so their means match, which removes the green cast that
        unbalanced Bayer demosaicing leaves behind. Returns an RGB uint8 array
        (alpha dropped). Used only when the camera has no hardware auto-WB."""
        import numpy as np
        rgb = arr[..., :3].astype(np.float32)
        means = rgb.reshape(-1, 3).mean(axis=0)
        means = np.maximum(means, 1e-3)
        target = means.mean()
        gains = np.clip(target / means, 0.25, 4.0)
        rgb *= gains
        return np.clip(rgb, 0, 255).astype(np.uint8)

    def _current_pixel_format(self) -> Optional[str]:
        try:
            return self._node_map.FindNode("PixelFormat").CurrentEntry().SymbolicValue()
        except Exception:
            return None

    def _set_stream_buffer_mode(self):
        """DEPRECATED / unused. Setting StreamBufferHandlingMode=NewestOnly
        destabilized the stream on the test host — a live exposure/gain write
        would then stall even the 10-bit feed. Left here (uncalled) as a record;
        the default buffer handling + manual drain in _grab_preview_jpeg is what
        streams reliably. Do not re-enable without testing on the rig."""
        return

    def _setup_pixel_format(self):
        """Select the raw Bayer streaming format. Prefers the configured
        ``camera_pixel_format`` (default BayerRG10g40IDS — the 10-bit format IDS
        Cockpit streams and the one that completes reliably on this USB3 host);
        the 12-bit BayerRG12g24IDS is available for hosts that can sustain the
        larger transfers but stalls with incomplete buffers on marginal links.
        Falls back to any offered 12-bit, then any Bayer, then the camera
        default. Whatever is chosen is repacked to an unpacked 16-bit container
        on save (see _raw_bayer_uint16). MUST run before PayloadSize is read
        (buffer size and stream bandwidth both depend on the pixel format)."""
        nm = self._node_map
        try:
            node = nm.FindNode("PixelFormat")
            entries = [e.SymbolicValue() for e in node.Entries() if e.IsAvailable()]
        except Exception as e:
            log.warning("PixelFormat enumerate failed (%s) — keeping default", e)
            self._pixel_format = self._current_pixel_format()
            return

        log.info("PixelFormat entries: %s", entries)
        preferred = getattr(self._settings, "camera_pixel_format", "") or ""
        bayer12 = [s for s in entries if s.startswith("Bayer") and "12" in s]
        if preferred and preferred in entries:
            choice = preferred
        elif preferred:
            log.warning("Configured PixelFormat %s not offered (have %s) — falling back",
                        preferred, entries)
            choice = (bayer12 or [None])[0]
        else:
            choice = (bayer12 or [None])[0]
        if not choice:
            log.warning("No usable Bayer format offered — keeping default (%s)",
                        self._current_pixel_format())
            self._pixel_format = self._current_pixel_format()
            return
        try:
            node.SetCurrentEntry(choice)
            self._pixel_format = choice
            packed = "g" in choice or choice.endswith("p")
            log.info("PixelFormat = %s (raw%s)", choice,
                     ", packed — will repack on save" if packed else "")
        except Exception as e:
            log.warning("Set PixelFormat=%s failed: %s — keeping default", choice, e)
            self._pixel_format = self._current_pixel_format()

    def _switch_pixel_format(self, target: str):
        """Retune the running stream to a different PixelFormat and restart it.
        PayloadSize changes with the format, so the announced buffers are
        revoked and re-allocated at the new size. Used to grab a single heavier
        (12-bit) frame for a raw TIFF while the live preview streams the lighter
        (10-bit) format — continuous 12-bit stalls this host, but a one-shot
        grab with the preview paused completes fine. Raises on failure so the
        caller can restore the stream format."""
        nm = self._node_map
        ds = self._data_stream
        try:
            nm.FindNode("AcquisitionStop").Execute()
            nm.FindNode("AcquisitionStop").WaitUntilDone()
        except Exception:
            pass
        ds.StopAcquisition()
        nm.FindNode("TLParamsLocked").SetValue(0)
        try:
            ds.Flush(self._ids_peak.DataStreamFlushMode_DiscardAll)
        except Exception:
            pass
        for buf in ds.AnnouncedBuffers():
            ds.RevokeBuffer(buf)
        nm.FindNode("PixelFormat").SetCurrentEntry(target)
        self._pixel_format = target
        payload_size = nm.FindNode("PayloadSize").Value()
        for _ in range(self._num_buffers):
            buf = ds.AllocAndAnnounceBuffer(payload_size)
            ds.QueueBuffer(buf)
        nm.FindNode("TLParamsLocked").SetValue(1)
        ds.StartAcquisition()
        nm.FindNode("AcquisitionStart").Execute()
        nm.FindNode("AcquisitionStart").WaitUntilDone()
        log.info("PixelFormat retuned to %s", target)

    def _resolve_conv_mode(self):
        """Bayer→BGRa8 conversion mode, resolved once and cached. HighQuality
        gives the best colour; some SDK builds only have Fast."""
        ipl = self._ids_ipl
        if self._conv_mode is None:
            self._conv_mode = getattr(ipl, "ConversionMode_HighQuality",
                                      ipl.ConversionMode_Fast)
        return self._conv_mode

    def _raw_bayer_uint16(self, image):
        """From an IPL image on the current (packed) Bayer format, return the
        raw mosaic as a 2-D uint16 NumPy array. Repacks a packed format
        (e.g. BayerRG10g40IDS, BayerRG12g24IDS) to its unpacked 16-bit variant
        (BayerRG10 / BayerRG12) first — lossless, no debayering. Copies out of
        buffer memory so the caller can requeue. No white balance and no flips:
        flipping a Bayer mosaic shifts its CFA phase, desyncing it from the
        recorded pixel_format — reorient offline."""
        import re
        import numpy as np
        ipl = self._ids_ipl
        fmt = self._pixel_format or ""
        # Packed Bayer names are "Bayer<phase><bits><suffix>" (suffix = g40IDS,
        # g24IDS, p, …); the unpacked container is "Bayer<phase><bits>".
        m = re.match(r"(Bayer[RGB]{2})(\d+)", fmt)
        if m and not fmt.endswith(m.group(2)):
            target = getattr(ipl, f"PixelFormatName_{m.group(1)}{m.group(2)}", None)
            if target is not None:
                try:
                    image = image.ConvertTo(target, ipl.ConversionMode_Fast)
                except Exception as e:
                    log.warning("Repack packed→unpacked %s%s failed: %s",
                                m.group(1), m.group(2), e)
        arr = np.array(self._ipl_to_numpy(image))  # copy out before requeue
        # get_numpy_3D hands back a 16-bit single-channel image as (H, W, 2)
        # little-endian bytes, so reinterpret each byte-pair as one uint16.
        if arr.ndim == 3 and arr.shape[-1] == 1:
            arr = arr[..., 0]
        elif arr.ndim == 3 and arr.shape[-1] == 2 and arr.dtype == np.uint8:
            h, w = arr.shape[:2]
            arr = np.ascontiguousarray(arr).view("<u2").reshape(h, w)
        if arr.ndim != 2:
            raise RuntimeError(
                f"Raw Bayer frame is not 2-D (shape {arr.shape}, fmt {self._pixel_format})")
        return np.ascontiguousarray(arr, dtype="<u2")

    def _debayered_rgb8(self, image):
        """From an IPL image (raw Bayer), debayer to 8-bit RGB for *viewing*:
        apply the same gray-world WB as the live preview (when active) and the
        geometric flips, so the companion PNG matches what the operator sees.
        Returns an HxWx3 uint8 array — a viewable aid, NOT measurement data."""
        import numpy as np
        ipl = self._ids_ipl
        color = image.ConvertTo(ipl.PixelFormatName_BGRa8, self._resolve_conv_mode())
        arr = np.array(self._ipl_to_numpy(color))  # copy out before requeue
        if arr.ndim == 3 and arr.shape[2] >= 3:
            arr = arr[..., [2, 1, 0]]  # BGR(A) → RGB
        arr = arr[..., :3]
        if self._wb_software:
            arr = self._apply_gray_world_wb(arr)
        if self._flip_x:
            arr = arr[:, ::-1]
        if self._flip_y:
            arr = arr[::-1]
        return np.ascontiguousarray(arr)

    @staticmethod
    def _write_tiff16(arr, output_path: str):
        """Write a 2-D uint16 array as a single-channel 16-bit TIFF."""
        from PIL import Image as PILImage
        h, w = arr.shape
        try:
            img = PILImage.fromarray(arr, mode="I;16")
        except Exception:
            # Some Pillow builds mis-handle fromarray for I;16 — go via raw bytes.
            img = PILImage.frombytes("I;16", (w, h), arr.tobytes())
        img.save(output_path, format="TIFF")

    def _save_image(self, image, output_path: str, fmt: str):
        """Save an IPL image. JPG/PNG/BMP are written natively by the SDK
        (format inferred from the file extension); TIFF (and any format the
        SDK cannot write) falls back to Pillow."""
        ipl = self._ids_ipl
        fmt = fmt.lower()
        if fmt in ("jpeg", "jpg"):
            ipl.ImageWriter.WriteAsJPG(output_path, image)
        elif fmt == "png":
            ipl.ImageWriter.WriteAsPNG(output_path, image)
        elif fmt == "bmp":
            ipl.ImageWriter.WriteAsBMP(output_path, image)
        else:
            # TIFF (and anything the SDK's ImageWriter can't produce) → Pillow.
            # The image is BGRa8, and the IPL converter does not support a
            # BGRa8→RGB8 conversion, so reorder channels in NumPy instead.
            from PIL import Image as PILImage
            arr = self._ipl_to_numpy(image)
            if arr.ndim == 3 and arr.shape[2] in (3, 4):
                arr = arr[..., [2, 1, 0]]  # BGR(A) → RGB, dropping alpha
            PILImage.fromarray(arr).save(output_path)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _setup_white_balance(self):
        """Enable the camera's hardware auto white balance. Node and enum-entry
        names vary across IDS models, so enumerate what this camera offers, log
        it, and try the auto entries in order. If none work, switch on the
        software gray-world fallback so the preview/captures are still balanced."""
        nm = self._node_map
        try:
            node = nm.FindNode("BalanceWhiteAuto")
        except Exception as e:
            log.warning("No BalanceWhiteAuto node (%s) — using software white balance", e)
            self._wb_software = True
            return

        try:
            entries = [e.SymbolicValue() for e in node.Entries()]
            log.info("BalanceWhiteAuto entries: %s", entries)
        except Exception:
            entries = []

        for entry in ("Continuous", "Once"):
            if entries and entry not in entries:
                continue
            try:
                node.SetCurrentEntry(entry)
                log.info("Auto white balance = %s (hardware)", entry)
                return
            except Exception as e:
                log.warning("BalanceWhiteAuto=%s failed: %s", entry, e)

        log.warning("Hardware auto white balance unavailable — using software gray-world WB")
        self._wb_software = True

    def _limit_usb_bandwidth(self):
        """Throttle the camera's USB3 output to avoid GC_ERR_TIMEOUT stalls.

        Applies an absolute DeviceLinkThroughputLimit (if configured) and/or an
        AcquisitionFrameRate cap. Every node touch is guarded independently: a
        camera that lacks a node, or rejects a value, logs and moves on — this
        must never break the (working) open path.
        """
        nm = self._node_map

        def _clamp(node, value):
            try:
                value = max(node.Minimum(), min(node.Maximum(), value))
            except Exception:
                pass
            return value

        # DeviceLinkThroughputLimit — hard ceiling on bytes/s over the link.
        limit_mbps = getattr(self._settings, "camera_throughput_limit_mbps", 0.0)
        if limit_mbps and limit_mbps > 0:
            try:
                try:
                    nm.FindNode("DeviceLinkThroughputLimitMode").SetCurrentEntry("On")
                except Exception:
                    pass  # some models have no mode node; the limit alone applies
                node = nm.FindNode("DeviceLinkThroughputLimit")
                target = _clamp(node, int(limit_mbps * 1_000_000))
                node.SetValue(target)
                log.info("DeviceLinkThroughputLimit set to %d B/s (~%.1f MB/s)",
                         target, target / 1_000_000)
            except Exception as e:
                log.warning("DeviceLinkThroughputLimit not applied: %s", e)

        # AcquisitionFrameRate — fewer frames/s = less sustained bandwidth.
        fps = getattr(self._settings, "camera_frame_rate_hz", 0.0)
        if fps and fps > 0:
            try:
                try:
                    nm.FindNode("AcquisitionFrameRateEnable").SetValue(True)
                except Exception:
                    pass  # node absent on some models; frame rate is always live
                node = nm.FindNode("AcquisitionFrameRate")
                target = _clamp(node, float(fps))
                node.SetValue(target)
                log.info("AcquisitionFrameRate capped to %.2f fps", target)
            except Exception as e:
                log.warning("AcquisitionFrameRate not applied: %s", e)

    def _open_first_camera(self):
        peak = self._ids_peak
        peak.Library.Initialize()
        device_manager = peak.DeviceManager.Instance()
        device_manager.Update()
        if device_manager.Devices().empty():
            raise RuntimeError("No IDS camera found")
        self._device = device_manager.Devices()[0].OpenDevice(
            peak.DeviceAccessType_Control
        )
        self._node_map = self._device.RemoteDevice().NodeMaps()[0]
        self._data_stream = self._device.DataStreams()[0].OpenDataStream()

        # Free-running acquisition (no external/software trigger).
        try:
            self._node_map.FindNode("TriggerMode").SetCurrentEntry("Off")
        except Exception:
            pass

        # Auto white balance — matches IDS Cockpit behaviour and eliminates
        # the green cast that unbalanced Bayer demosaicing produces. Falls back
        # to a software gray-world correction if the camera has no auto-WB node.
        self._setup_white_balance()

        # Switch to an unpacked 12-bit Bayer format for full-depth raw TIFFs.
        # Must precede PayloadSize (buffer size depends on the pixel format).
        self._setup_pixel_format()

        # Cap USB bandwidth to guard against GC_ERR_TIMEOUT mid-stream stalls.
        # Must follow the pixel-format change (throughput scales with depth) and
        # precede TLParamsLocked (these nodes lock during acquisition).
        self._limit_usb_bandwidth()

        payload_size = self._node_map.FindNode("PayloadSize").Value()
        # 12-bit packed frames are ~2× the bytes of 8-bit. Allocate at least 16
        # buffers so a slow preview encode (200–500 ms on a laptop) never exhausts
        # the queue and forces the camera to overwrite an in-use buffer — which
        # is what produces the torn/half-dark frames.
        num_buffers = max(self._data_stream.NumBuffersAnnouncedMinRequired(), 16)
        self._num_buffers = num_buffers
        for _ in range(num_buffers):
            buf = self._data_stream.AllocAndAnnounceBuffer(payload_size)
            self._data_stream.QueueBuffer(buf)

        # Order matters (per IDS peak samples): lock the parameters, start the
        # host-side stream, then start the device acquisition and wait for the
        # command to complete.
        self._node_map.FindNode("TLParamsLocked").SetValue(1)
        self._data_stream.StartAcquisition()
        self._node_map.FindNode("AcquisitionStart").Execute()
        self._node_map.FindNode("AcquisitionStart").WaitUntilDone()
        self._open = True
        log.info("IDS camera opened: %s", self._device.SerialNumber())

    async def open(self) -> bool:
        if self._open:
            return True  # already open; opening again would fail (single-process device)
        if not self._load_sdk():
            return False
        try:
            await asyncio.get_event_loop().run_in_executor(_executor, self._open_first_camera)
            return True
        except Exception as e:
            log.error("Camera open failed: %s", e)
            return False

    def _close_camera(self):
        if not self._open:
            return
        try:
            self._node_map.FindNode("AcquisitionStop").Execute()
            self._node_map.FindNode("TLParamsLocked").SetValue(0)
            self._data_stream.StopAcquisition()
            self._data_stream.Flush(self._ids_peak.DataStreamFlushMode_DiscardAll)
            for buf in self._data_stream.AnnouncedBuffers():
                self._data_stream.RevokeBuffer(buf)
        except Exception as e:
            log.warning("Camera close error: %s", e)
        finally:
            self._open = False
            self._ids_peak.Library.Close()

    async def close(self):
        await asyncio.get_event_loop().run_in_executor(_executor, self._close_camera)

    # ── USB-stall auto-recovery ───────────────────────────────────────────────

    @staticmethod
    def _is_timeout_error(e) -> bool:
        """True for the GC_ERR_TIMEOUT / PEAK_RETURN_CODE_TIMEOUT that the SDK
        raises when the camera stops delivering frames (or ACKing control
        writes) on a stalled USB link."""
        s = str(e).upper()
        return ("GC_ERR_TIMEOUT" in s or "PEAK_RETURN_CODE_TIMEOUT" in s
                or "TIMED OUT" in s)

    def _restart_acquisition(self):
        """Cheap recovery: bounce the data stream without reopening the device.
        Keeps TLParamsLocked/buffers as-is — just stops, flushes, re-queues the
        announced buffers, and starts again. Any step may itself time out if the
        USB link is fully wedged; the caller escalates to a full reopen then."""
        ds = self._data_stream
        nm = self._node_map
        for node in ("AcquisitionStop",):
            try:
                nm.FindNode(node).Execute()
                nm.FindNode(node).WaitUntilDone()
            except Exception:
                pass
        try:
            ds.StopAcquisition()
        except Exception:
            pass
        try:
            ds.Flush(self._ids_peak.DataStreamFlushMode_DiscardAll)
        except Exception:
            pass
        try:
            for buf in ds.AnnouncedBuffers():
                try:
                    ds.QueueBuffer(buf)
                except Exception:
                    pass  # already queued
        except Exception:
            pass
        # These two MUST succeed for the stream to be live again — let them raise
        # so _recover() escalates to a reopen on failure.
        ds.StartAcquisition()
        nm.FindNode("AcquisitionStart").Execute()
        nm.FindNode("AcquisitionStart").WaitUntilDone()

    def _reopen_device(self):
        """Expensive recovery: fully close and re-open the camera. Works when
        Windows still has the device enumerated; if the USB stack dropped it
        entirely, _open_first_camera raises 'No IDS camera found' and a physical
        replug is genuinely required."""
        self._close_camera()
        self._open_first_camera()

    def _recover(self) -> bool:
        """Bring the stream back after a USB timeout stall: try a stream restart
        first, then a full device reopen. Rate-limited by _recover_cooldown_s so
        the preview loop can't spin on a dead camera. If a restart didn't
        actually revive the stream (we're back here within _recover_escalate_s),
        skip straight to the reopen. Runs on the executor thread (same as every
        other SDK call), so it can't race a grab."""
        import time
        now = time.monotonic()
        # Never run heavy recovery (stop/start, reopen) while we're deliberately
        # retuning the pixel format for a capture — a transient timeout there is
        # expected and recovery would fight the switch and can wedge the link.
        if self._in_format_switch:
            return False
        if now - self._last_recover_ts < self._recover_cooldown_s:
            return False
        # A stall soon after a "successful" restart means the restart didn't
        # really fix it — don't keep shallow-restarting, escalate to reopen.
        restart_ineffective = (self._last_recover_was_restart
                               and now - self._last_recover_ts < self._recover_escalate_s)
        self._last_recover_ts = now

        if not restart_ineffective:
            log.warning("USB stall detected — restarting acquisition")
            try:
                self._restart_acquisition()
                self._last_recover_was_restart = True
                log.info("Acquisition restart succeeded")
                return True
            except Exception as e:
                log.warning("Acquisition restart failed (%s) — reopening device", e)

        try:
            self._reopen_device()
            self._last_recover_was_restart = False
            log.info("Device reopen succeeded")
            return True
        except Exception as e:
            self._last_recover_was_restart = False
            log.error("Device reopen failed (%s) — physical USB replug required", e)
            return False

    # ── Capture ───────────────────────────────────────────────────────────────

    def _ensure_streaming(self, fmt: str):
        """Best-effort: get the data stream running at `fmt` from whatever state
        a (possibly failed) switch left it in. Never raises — a genuinely dead
        control channel can't be revived here, but that's logged, not thrown, so
        a capture never leaves the app spinning."""
        try:
            self._switch_pixel_format(fmt)
        except Exception as e:
            log.error("Could not restore stream to %s: %s", fmt, e)

    def _capture_frame(self, output_path: str, image_format: str, metadata: dict) -> str:
        fmt = image_format.lower()
        if fmt not in ("tiff", "png", "bmp", "jpeg", "jpg"):
            fmt = "tiff"

        # A raw TIFF is written at the (heavier) capture format for full depth,
        # while the live preview streams the lighter camera_pixel_format. Retune
        # to the capture format for this one grab, then restore the stream format.
        # Continuous 12-bit stalls this host, but a one-shot grab (preview paused
        # by the caller) completes fine — the frames Cockpit also captures.
        stream_fmt = self._pixel_format
        capture_fmt = getattr(self._settings, "camera_capture_pixel_format", "") or ""
        if not (fmt == "tiff" and capture_fmt and capture_fmt != stream_fmt):
            return self._capture_at_current_format(fmt, output_path, metadata)

        # Switched capture. _recover() is gated off for the duration (see
        # _in_format_switch) so a switch hiccup can't cascade into a reopen/wedge;
        # on ANY failure we restore the stream format and fall back to capturing
        # there, so a Save always produces a file and the preview always survives.
        self._in_format_switch = True
        restored = False
        try:
            try:
                self._switch_pixel_format(capture_fmt)
            except Exception as e:
                log.warning("Capture switch to %s failed (%s) — capturing at %s instead",
                            capture_fmt, e, stream_fmt)
                self._ensure_streaming(stream_fmt)
                restored = True
                return self._capture_at_current_format(fmt, output_path, metadata)
            try:
                return self._capture_at_current_format(fmt, output_path, metadata)
            except Exception as e:
                log.warning("Capture at %s failed after switch (%s) — falling back to %s",
                            capture_fmt, e, stream_fmt)
                self._ensure_streaming(stream_fmt)
                restored = True
                return self._capture_at_current_format(fmt, output_path, metadata)
        finally:
            if not restored:
                self._ensure_streaming(stream_fmt)
            self._in_format_switch = False

    def _capture_at_current_format(self, fmt: str, output_path: str, metadata: dict) -> str:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Discard incomplete (torn) buffers — rare, but possible if the preview
        # consumer was briefly starving the queue. Retry up to 3 times. A USB
        # stall raises GC_ERR_TIMEOUT instead of returning a buffer; bounce the
        # stream once and retry so a scan capture self-heals like the preview.
        for attempt in range(3):
            try:
                raw_buffer = self._data_stream.WaitForFinishedBuffer(5000)
            except Exception as e:
                if self._is_timeout_error(e) and self._recover():
                    continue
                raise
            if not getattr(raw_buffer, "IsIncomplete", lambda: False)():
                break
            log.warning("Capture: incomplete buffer (attempt %d), retrying", attempt + 1)
            self._data_stream.QueueBuffer(raw_buffer)
        else:
            raise RuntimeError("Could not obtain a complete frame after 3 attempts")

        if fmt == "tiff":
            # Full-depth raw Bayer mosaic — single-channel 16-bit, no debayer /
            # WB / colour conversion, so the pixels keep their true sensor
            # values (0–4095) for quantitative measurement (debayer offline).
            # Alongside it, write a viewable 8-bit PNG (debayered + gray-world
            # WB'd, matching the live preview) so the operator can see the shot.
            # Both are derived from the SAME buffer before its single requeue.
            from PIL import Image as PILImage
            try:
                image = self._ids_ipl_ext.BufferToImage(raw_buffer)
                raw_arr = self._raw_bayer_uint16(image)      # 16-bit mosaic (copy)
                view_arr = self._debayered_rgb8(image)       # 8-bit RGB view (copy)
            finally:
                self._data_stream.QueueBuffer(raw_buffer)
            self._write_tiff16(raw_arr, str(path))
            png_path = path.with_suffix(".png")
            PILImage.fromarray(view_arr).save(str(png_path), format="PNG")
            metadata.setdefault("pixel_format", self._pixel_format)
            metadata.setdefault("raw_bayer", True)
            # Real ADC depth of the streamed format (10 or 12), parsed from its
            # name; the 16-bit TIFF container holds these values left-unshifted.
            import re as _re
            _m = _re.match(r"Bayer[RGB]{2}(\d+)", self._pixel_format or "")
            metadata.setdefault("bit_depth", int(_m.group(1)) if _m else None)
            metadata.setdefault("white_balance",
                                "none (raw mosaic); companion .png is WB'd 8-bit")
            metadata.setdefault("preview_png", png_path.name)
        else:
            # Viewable colour formats: debayer to BGRa8. Still RAW radiometrically
            # (no white balance) — only geometric flips are applied.
            ipl_image = self._buffer_to_color_image(raw_buffer)
            if self._flip_x or self._flip_y:
                from PIL import Image as PILImage
                arr = self._ipl_to_numpy(ipl_image)
                if arr.ndim == 3 and arr.shape[2] >= 3:
                    arr = arr[..., [2, 1, 0]]
                if self._flip_x:
                    arr = arr[:, ::-1]
                if self._flip_y:
                    arr = arr[::-1]
                pil_img = PILImage.fromarray(arr[..., :3].copy())
                fmt_map = {"jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "bmp": "BMP"}
                save_kw = {"quality": 95} if fmt in ("jpeg", "jpg") else {}
                pil_img.save(str(path), format=fmt_map.get(fmt, "PNG"), **save_kw)
            else:
                self._save_image(ipl_image, str(path), fmt)

        # Save sidecar metadata JSON
        meta_path = path.with_suffix(".json")
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)

        log.info("Image saved: %s", path)
        return str(path)

    async def capture(self, output_path: str, image_format: str, metadata: dict) -> Optional[str]:
        """Capture one frame, save it, return the file path or None on failure."""
        if not self._open:
            log.error("Camera not open")
            return None
        try:
            return await asyncio.get_event_loop().run_in_executor(
                _executor,
                self._capture_frame,
                output_path,
                image_format,
                metadata,
            )
        except Exception as e:
            log.error("Capture failed: %s", e)
            return None

    # ── Preview (JPEG thumbnail for browser) ─────────────────────────────────

    def _grab_preview_jpeg(self) -> Optional[bytes]:
        try:
            import io
            from PIL import Image as PILImage
            ipl = self._ids_ipl

            # Grab the freshest COMPLETE frame. Default buffer handling hands
            # back the OLDEST finished buffer, so drain to the newest queued one
            # first (a slow JPEG encode otherwise shows a stale burst → flicker).
            # Skip INCOMPLETE (torn) buffers. On a USB stall WaitForFinishedBuffer
            # raises a timeout; self-heal once and skip this grab.
            raw_buffer = None
            for _ in range(3):
                try:
                    buf = self._data_stream.WaitForFinishedBuffer(2000)
                except Exception as e:
                    if self._is_timeout_error(e):
                        self._recover()
                    return None
                while True:
                    try:
                        stale = self._data_stream.WaitForFinishedBuffer(5)
                        self._data_stream.QueueBuffer(buf)
                        buf = stale
                    except Exception:
                        break  # queue empty — buf is the newest frame
                if getattr(buf, "IsIncomplete", lambda: False)():
                    self._data_stream.QueueBuffer(buf)
                    continue  # torn frame — try again for a complete one
                raw_buffer = buf
                break
            if raw_buffer is None:
                return None  # only torn frames available — skip this grab

            # ConversionMode_Fast doesn't handle the IDS packed Bayer format
            # correctly on this camera — produces a black frame. Use the same
            # mode as captures (HighQuality if available, else Fast).
            image = self._ids_ipl_ext.BufferToImage(raw_buffer)
            ipl_image = image.ConvertTo(ipl.PixelFormatName_BGRa8, self._resolve_conv_mode())
            self._data_stream.QueueBuffer(raw_buffer)

            # Scale in SDK space first (native code on the full-res IPL image),
            # so the numpy copy and Pillow JPEG encode work on a small array.
            PREVIEW_W = 640
            src_w, src_h = ipl_image.Width(), ipl_image.Height()
            if src_w > PREVIEW_W:
                size = ipl.Size2D()
                size.width = PREVIEW_W
                size.height = max(1, round(src_h * PREVIEW_W / src_w))
                small = ipl_image.Scale(size)
            else:
                small = ipl_image

            arr = self._ipl_to_numpy(small)
            # BGRa8 → RGB: select channels [2,1,0] = R,G,B (alpha ignored)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                arr = arr[..., [2, 1, 0]]
            if self._wb_software:
                arr = self._apply_gray_world_wb(arr)
            if self._flip_x:
                arr = arr[:, ::-1]
            if self._flip_y:
                arr = arr[::-1]
            buf = io.BytesIO()
            PILImage.fromarray(arr.copy()).save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        except Exception as e:
            log.warning("Preview grab failed: %s", e)
            # A USB stall (no frames on the link) leaves the stream dead until
            # it's bounced. Self-heal so the operator doesn't have to replug the
            # cable; rate-limited inside _recover so we don't spin at 15 fps.
            if self._is_timeout_error(e):
                self._recover()
            return None

    async def grab_preview_jpeg(self) -> Optional[bytes]:
        if not self._open:
            return None
        return await asyncio.get_event_loop().run_in_executor(_executor, self._grab_preview_jpeg)

    @property
    def is_open(self) -> bool:
        return self._open

    # ── Properties (node map accessors) ──────────────────────────────────────

    def _get_camera_info(self) -> dict:
        if not self._open:
            return {}
        try:
            nm = self._node_map
            return {
                "model": self._device.ModelName(),
                "serial": self._device.SerialNumber(),
                "exposure_us": nm.FindNode("ExposureTime").Value(),
                "gain": nm.FindNode("Gain").Value(),
                "width": nm.FindNode("Width").Value(),
                "height": nm.FindNode("Height").Value(),
                "pixel_format": nm.FindNode("PixelFormat").CurrentEntry().SymbolicValue(),
            }
        except Exception as e:
            log.warning("get_camera_info error: %s", e)
            return {}

    async def get_info(self) -> dict:
        return await asyncio.get_event_loop().run_in_executor(_executor, self._get_camera_info)

    def _set_exposure(self, exposure_us: float):
        self._node_map.FindNode("ExposureTime").SetValue(exposure_us)

    async def set_exposure(self, exposure_us: float):
        if self._open:
            await asyncio.get_event_loop().run_in_executor(
                _executor, self._set_exposure, exposure_us
            )

    def _set_gain(self, gain: float):
        node = self._node_map.FindNode("Gain")
        try:
            lo = node.Minimum()
            hi = node.Maximum()
            gain = max(lo, min(hi, gain))
        except Exception:
            pass
        node.SetValue(gain)

    async def set_gain(self, gain: float):
        if self._open:
            await asyncio.get_event_loop().run_in_executor(
                _executor, self._set_gain, gain
            )

    def _set_reverse(self, reverse_x: Optional[bool], reverse_y: Optional[bool]):
        # ReverseX/Y nodes are often locked during acquisition; use software flip
        # as the reliable fallback (applied in preview and capture via numpy).
        if reverse_x is not None:
            self._flip_x = reverse_x
            log.info("Mirror X = %s (software)", reverse_x)
        if reverse_y is not None:
            self._flip_y = reverse_y
            log.info("Flip Y = %s (software)", reverse_y)

    async def set_reverse(self, reverse_x: Optional[bool] = None,
                          reverse_y: Optional[bool] = None):
        if self._open:
            await asyncio.get_event_loop().run_in_executor(
                _executor, self._set_reverse, reverse_x, reverse_y
            )

    @property
    def is_open(self) -> bool:
        return self._open
