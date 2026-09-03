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
        self._pixel_format = None     # camera's current PixelFormat symbolic name

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

    @staticmethod
    def _bayer_phase(fmt_name: str) -> Optional[str]:
        """CFA phase of a Bayer PixelFormat name ('BayerRG12p' → 'RG'), or
        None if the name is not a Bayer format."""
        if not fmt_name or not fmt_name.startswith("Bayer"):
            return None
        phase = fmt_name[5:7]
        return phase if phase in ("RG", "GR", "GB", "BG") else None

    def _save_raw_bayer_tiff(self, raw_buffer, output_path: str):
        """Save the RAW Bayer mosaic (no debayering, no white balance, no
        colour conversion) as a 16-bit single-channel TIFF — the sensor's
        true readout, with the CFA pattern intact for offline processing.

        The acquisition buffer holds a Bayer frame (12 significant bits when
        the sensor is in a Bayer*12 format). Packed formats (…12p / …12g24IDS)
        are unpacked to the plain 16-bit twin of the same CFA phase first; that
        Bayer→same-CFA-Bayer step only re-lays the bits, it does not
        interpolate colour. Then the pixels go straight to disk."""
        import numpy as np
        from PIL import Image as PILImage
        ipl = self._ids_ipl
        image = self._ids_ipl_ext.BufferToImage(raw_buffer)

        # Unpack to a 16-bit Bayer container so the pixels arrive as a plain
        # 2-D array. Converting an already-unpacked Bayer*12 to itself is a
        # harmless no-op; skip entirely for non-Bayer sensors (mono/colour).
        phase = self._bayer_phase(self._pixel_format or "")
        if phase is not None:
            target = getattr(ipl, f"PixelFormatName_Bayer{phase}12", None)
            if target is not None:
                try:
                    image = image.ConvertTo(target, ipl.ConversionMode_Fast)
                except Exception as e:
                    log.warning("Raw Bayer unpack (%s) failed, saving as-is: %s",
                                target, e)

        # Copy out of the SDK image before requeuing — QueueBuffer may recycle
        # the underlying memory immediately.
        arr = np.array(self._ipl_to_numpy(image))
        self._data_stream.QueueBuffer(raw_buffer)

        if arr.ndim == 3 and arr.shape[-1] == 1:
            arr = arr[..., 0]
        if arr.ndim != 2:
            raise RuntimeError(
                f"Raw Bayer frame is not 2-D (shape {arr.shape}, "
                f"format {self._pixel_format}) — cannot save as mosaic TIFF"
            )
        if arr.dtype != np.uint16:
            arr = arr.astype(np.uint16)
        # Little-endian, contiguous — required for a clean 16-bit write.
        arr = np.ascontiguousarray(arr, dtype="<u2")
        h, w = arr.shape
        # mode 'I;16' → single-channel 16-bit TIFF; the low 12 bits hold data.
        # fromarray with an explicit mode is rejected by some Pillow builds
        # (stride/endianness checks), so fall back to frombytes.
        try:
            img = PILImage.fromarray(arr, mode="I;16")
        except Exception:
            img = PILImage.frombytes("I;16", (w, h), arr.tobytes())
        img.save(output_path, format="TIFF")

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

        for entry in ("Once", "Continuous"):
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

    def _setup_manual_exposure(self):
        """Turn OFF auto-exposure and auto-gain so brightness is fixed and
        user-controlled (via the Exposure/Gain tab). Auto modes re-evaluate
        every frame, which makes the live preview pulse bright/dark and — worse
        for a gonioreflectometer — would vary the exposure between scan
        positions, destroying the radiometric comparability of the captures.
        Node names are model-specific, so this is best-effort."""
        nm = self._node_map
        for node_name in ("ExposureAuto", "GainAuto"):
            try:
                node = nm.FindNode(node_name)
            except Exception:
                continue
            try:
                node.SetCurrentEntry("Off")
                log.info("%s = Off (manual)", node_name)
            except Exception as e:
                log.warning("%s=Off failed: %s", node_name, e)

    def _setup_raw_bayer_format(self):
        """Put the sensor into a raw 12-bit Bayer pixel format so TIFF captures
        keep the full sensor bit depth and the untouched CFA mosaic. Prefer an
        unpacked Bayer*12 (16-bit container, trivial to save); fall back to a
        packed 12-bit variant (unpacked at save time), then leave the format
        as-is. The colour preview still debayers this to BGRa8 on the fly, so
        this does not affect the live view. Must run before PayloadSize is read
        (buffer sizing depends on the pixel format)."""
        nm = self._node_map
        try:
            node = nm.FindNode("PixelFormat")
            entries = [e.SymbolicValue() for e in node.Entries()]
        except Exception as e:
            log.warning("Cannot enumerate PixelFormat (%s) — leaving default", e)
            return

        bayer12 = [s for s in entries
                   if s.startswith("Bayer") and "12" in s and self._bayer_phase(s)]
        unpacked = [s for s in bayer12 if not (s.endswith("p") or s.endswith("IDS"))]
        ordered = unpacked + [s for s in bayer12 if s not in unpacked]
        for entry in ordered:
            try:
                node.SetCurrentEntry(entry)
                self._pixel_format = entry
                log.info("Pixel format = %s (raw Bayer 12-bit for capture)", entry)
                return
            except Exception as ex:
                log.warning("PixelFormat=%s failed: %s", entry, ex)

        # No raw Bayer 12-bit available — record whatever the camera is using so
        # the capture path can still decide how to save it.
        try:
            self._pixel_format = node.CurrentEntry().SymbolicValue()
        except Exception:
            self._pixel_format = None
        log.info("No raw Bayer 12-bit pixel format; using %s (available: %s)",
                 self._pixel_format, entries)

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

        # Fixed exposure/gain (no auto hunting → stable brightness, comparable
        # captures). Do this before locking parameters / starting acquisition.
        self._setup_manual_exposure()

        # Raw 12-bit Bayer for capture (must precede PayloadSize/buffer sizing).
        self._setup_raw_bayer_format()

        payload_size = self._node_map.FindNode("PayloadSize").Value()
        # Allocate several buffers, not just the bare minimum. With only 1–2
        # buffers the host has no double-buffering headroom, so at the higher
        # 12-bit data rate frames get overwritten mid-transfer — which shows up
        # as torn frames (one half bright, one half dark) and flicker in the
        # preview. A small pool absorbs the jitter.
        num_buffers = max(self._data_stream.NumBuffersAnnouncedMinRequired(), 8)
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
        raw_buffer = self._data_stream.WaitForFinishedBuffer(5000)

        fmt = image_format.lower()
        if fmt not in ("tiff", "png", "bmp", "jpeg", "jpg"):
            fmt = "tiff"

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "tiff":
            # RAW path: save the Bayer mosaic exactly as the sensor read it —
            # no debayering, no white balance, no colour conversion, no flips
            # (a 1-px flip would shift the CFA phase and corrupt downstream
            # demosaicing). This is the radiometric ground truth. Records the
            # pixel format in the sidecar so the mosaic can be decoded later.
            self._save_raw_bayer_tiff(raw_buffer, str(path))  # requeues buffer
            metadata.setdefault("pixel_format", self._pixel_format)
            metadata.setdefault("raw_bayer", True)
        else:
            # Viewable formats (jpg/png/bmp): debayer to colour. No white
            # balance is applied so gains stay constant across scan positions;
            # only geometric flips (lossless) go through NumPy.
            ipl_image = self._buffer_to_color_image(raw_buffer)  # requeues buffer
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
            log.exception("Capture failed: %s", e)
            return None

    # ── Preview (JPEG thumbnail for browser) ─────────────────────────────────

    def _grab_preview_jpeg(self) -> Optional[bytes]:
        try:
            import io
            from PIL import Image as PILImage
            ipl = self._ids_ipl
            raw_buffer = self._data_stream.WaitForFinishedBuffer(2000)
            ipl_image = self._buffer_to_color_image(raw_buffer)

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
        self._node_map.FindNode("Gain").SetValue(gain)

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
