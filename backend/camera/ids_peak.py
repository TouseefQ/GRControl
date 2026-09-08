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
_executor = ThreadPoolExecutor(max_workers=2)


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

    def __init__(self):
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

    def _setup_raw_bayer_12(self):
        """Switch the sensor to an *unpacked* 12-bit Bayer format so TIFF
        captures preserve the full sensor depth (0–4095) as a single-channel
        16-bit mosaic. Enumerate what the camera offers, log it, and prefer an
        unpacked ``BayerRG12`` (16-bit container — trivial to write as a 16-bit
        TIFF and to debayer offline); fall back to any 12-bit Bayer, else leave
        the default. MUST run before PayloadSize is read (the buffer size and
        stream bandwidth both depend on the pixel format)."""
        nm = self._node_map
        try:
            node = nm.FindNode("PixelFormat")
            entries = [e.SymbolicValue() for e in node.Entries() if e.IsAvailable()]
        except Exception as e:
            log.warning("PixelFormat enumerate failed (%s) — keeping default", e)
            self._pixel_format = self._current_pixel_format()
            return

        log.info("PixelFormat entries: %s", entries)
        bayer12 = [s for s in entries if s.startswith("Bayer") and "12" in s]
        # Unpacked names end in the bare bit count ("BayerRG12"); packed variants
        # carry a suffix ("BayerRG12p", "BayerRG12g24IDS") and are skipped here.
        unpacked = [s for s in bayer12 if s.endswith("12")]
        choice = (unpacked or bayer12 or [None])[0]
        if not choice:
            log.warning("No 12-bit Bayer format offered — keeping default (%s)",
                        self._current_pixel_format())
            self._pixel_format = self._current_pixel_format()
            return
        try:
            node.SetCurrentEntry(choice)
            self._pixel_format = choice
            log.info("PixelFormat = %s (raw 12-bit%s)", choice,
                     "" if choice.endswith("12") else ", packed — will repack on save")
        except Exception as e:
            log.warning("Set PixelFormat=%s failed: %s — keeping default", choice, e)
            self._pixel_format = self._current_pixel_format()

    def _resolve_conv_mode(self):
        """Bayer→BGRa8 conversion mode, resolved once and cached. HighQuality
        gives the best colour; some SDK builds only have Fast."""
        ipl = self._ids_ipl
        if self._conv_mode is None:
            self._conv_mode = getattr(ipl, "ConversionMode_HighQuality",
                                      ipl.ConversionMode_Fast)
        return self._conv_mode

    def _raw_bayer_uint16(self, image):
        """From an IPL image on the current (packed 12-bit) Bayer format, return
        the raw mosaic as a 2-D uint16 NumPy array (values 0–4095). Repacks a
        packed format to its unpacked 16-bit variant first — lossless, no
        debayering. Copies out of buffer memory so the caller can requeue.
        No white balance and no flips: flipping a Bayer mosaic shifts its CFA
        phase, desyncing it from the recorded pixel_format — reorient offline."""
        import numpy as np
        ipl = self._ids_ipl
        fmt = self._pixel_format or ""
        if fmt.startswith("Bayer") and "12" in fmt and not fmt.endswith("12"):
            phase = fmt[5:7]
            target = getattr(ipl, f"PixelFormatName_Bayer{phase}12", None)
            if target is not None:
                try:
                    image = image.ConvertTo(target, ipl.ConversionMode_Fast)
                except Exception as e:
                    log.warning("Repack packed→unpacked Bayer12 failed: %s", e)
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
        self._setup_raw_bayer_12()

        payload_size = self._node_map.FindNode("PayloadSize").Value()
        # 12-bit packed frames are ~2× the bytes of 8-bit. Allocate at least 16
        # buffers so a slow preview encode (200–500 ms on a laptop) never exhausts
        # the queue and forces the camera to overwrite an in-use buffer — which
        # is what produces the torn/half-dark frames.
        num_buffers = max(self._data_stream.NumBuffersAnnouncedMinRequired(), 16)
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

    # ── Capture ───────────────────────────────────────────────────────────────

    def _capture_frame(self, output_path: str, image_format: str, metadata: dict) -> str:
        fmt = image_format.lower()
        if fmt not in ("tiff", "png", "bmp", "jpeg", "jpg"):
            fmt = "tiff"

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Discard incomplete (torn) buffers — rare, but possible if the preview
        # consumer was briefly starving the queue. Retry up to 3 times.
        for attempt in range(3):
            raw_buffer = self._data_stream.WaitForFinishedBuffer(5000)
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
            metadata.setdefault("bit_depth", 12)
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

            # Drain any stale frames: slow conversion lets buffers pile up,
            # then they all drain in a burst → flicker.  Always grab the
            # freshest frame available before starting the expensive decode.
            raw_buffer = self._data_stream.WaitForFinishedBuffer(2000)
            while True:
                try:
                    stale = self._data_stream.WaitForFinishedBuffer(5)
                    self._data_stream.QueueBuffer(raw_buffer)
                    raw_buffer = stale
                except Exception:
                    break  # queue empty — raw_buffer is the newest frame

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
