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
        # HQ mode produces correct colour from the Bayer pattern; fall back to
        # Fast if this SDK version doesn't expose ConversionMode_HQ.
        mode = getattr(ipl, "ConversionMode_HQ",
                       getattr(ipl, "ConversionMode_HighQuality",
                               ipl.ConversionMode_Fast))
        log.debug("Bayer conversion mode: %s", mode)
        converted = image.ConvertTo(ipl.PixelFormatName_BGRa8, mode)
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

        payload_size = self._node_map.FindNode("PayloadSize").Value()
        for _ in range(self._data_stream.NumBuffersAnnouncedMinRequired()):
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
        ipl_image = self._buffer_to_color_image(raw_buffer)

        fmt = image_format.lower()
        if fmt not in ("tiff", "png", "bmp", "jpeg", "jpg"):
            fmt = "tiff"

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Save image
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
            ipl = self._ids_ipl
            raw_buffer = self._data_stream.WaitForFinishedBuffer(2000)
            ipl_image = self._buffer_to_color_image(raw_buffer)

            # Scale down to ~640px wide for preview. IPL's Scale() takes a
            # Size2D of target *pixel dimensions* (not scale factors); Size2D
            # is built via its settable width/height properties.
            PREVIEW_W = 640
            src_w, src_h = ipl_image.Width(), ipl_image.Height()
            if src_w > PREVIEW_W:
                size = ipl.Size2D()
                size.width = PREVIEW_W
                size.height = max(1, round(src_h * PREVIEW_W / src_w))
                small = ipl_image.Scale(size)
            else:
                small = ipl_image

            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                ipl.ImageWriter.WriteAsJPG(tmp_path, small)
                with open(tmp_path, "rb") as f:
                    data = f.read()
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            return data
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
        for node_name, value in (("ReverseX", reverse_x), ("ReverseY", reverse_y)):
            if value is None:
                continue
            try:
                self._node_map.FindNode(node_name).SetValue(value)
            except Exception as e:
                log.warning("set %s failed: %s", node_name, e)

    async def set_reverse(self, reverse_x: Optional[bool] = None,
                          reverse_y: Optional[bool] = None):
        if self._open:
            await asyncio.get_event_loop().run_in_executor(
                _executor, self._set_reverse, reverse_x, reverse_y
            )

    @property
    def is_open(self) -> bool:
        return self._open
