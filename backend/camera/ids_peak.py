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

    def _load_sdk(self):
        try:
            import ids_peak
            import ids_peak_ipl as ids_ipl
            self._ids_peak = ids_peak
            self._ids_ipl = ids_ipl
            return True
        except ImportError:
            log.warning("ids_peak SDK not found — camera features disabled")
            return False

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
        payload_size = self._node_map.FindNode("PayloadSize").Value()
        for _ in range(self._data_stream.NumBuffersAnnouncedMinRequired()):
            buf = self._data_stream.AllocAndAnnounceBuffer(payload_size)
            self._data_stream.QueueBuffer(buf)
        self._data_stream.StartAcquisition()
        self._node_map.FindNode("TLParamsLocked").SetValue(1)
        self._node_map.FindNode("AcquisitionStart").Execute()
        self._open = True
        log.info("IDS camera opened: %s", self._device.SerialNumber())

    async def open(self) -> bool:
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
        peak = self._ids_peak
        ipl = self._ids_ipl

        buffer = self._data_stream.WaitForFinishedBuffer(5000)
        ipl_image = ipl.Image.CreateFromSizeAndFormat(
            buffer.Width(), buffer.Height(), ipl.PixelFormatName_BGRa8
        )
        ipl_image.ConvertTo(
            ipl.PixelFormatName_BGRa8,
            ipl.ConversionMode_Fast
        )
        self._data_stream.QueueBuffer(buffer)

        fmt = image_format.lower()
        if fmt not in ("tiff", "png", "bmp", "jpeg", "jpg"):
            fmt = "tiff"

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Save image
        if fmt == "tiff":
            ipl_image.Save(str(path), ipl.ImageFileFormat_Tiff)
        elif fmt == "png":
            ipl_image.Save(str(path), ipl.ImageFileFormat_Png)
        elif fmt in ("jpeg", "jpg"):
            ipl_image.Save(str(path), ipl.ImageFileFormat_Jpeg)
        elif fmt == "bmp":
            ipl_image.Save(str(path), ipl.ImageFileFormat_Bmp)

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
            peak = self._ids_peak
            ipl = self._ids_ipl
            buffer = self._data_stream.WaitForFinishedBuffer(2000)
            ipl_image = ipl.Image.CreateFromSizeAndFormat(
                buffer.Width(), buffer.Height(), ipl.PixelFormatName_BGRa8
            )
            ipl_image.ConvertTo(ipl.PixelFormatName_BGRa8, ipl.ConversionMode_Fast)
            self._data_stream.QueueBuffer(buffer)

            # Scale down to 640px wide for preview
            scale = 640.0 / buffer.Width()
            w = int(buffer.Width() * scale)
            h = int(buffer.Height() * scale)
            small = ipl_image.Scale(w, h)

            import tempfile, base64
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                small.Save(tmp.name, ipl.ImageFileFormat_Jpeg)
                tmp_path = tmp.name
            with open(tmp_path, "rb") as f:
                data = f.read()
            os.unlink(tmp_path)
            return data
        except Exception as e:
            log.warning("Preview grab failed: %s", e)
            return None

    async def grab_preview_jpeg(self) -> Optional[bytes]:
        if not self._open:
            return None
        return await asyncio.get_event_loop().run_in_executor(_executor, self._grab_preview_jpeg)

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

    @property
    def is_open(self) -> bool:
        return self._open
