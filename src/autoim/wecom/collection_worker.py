"""One restartable read-only process for the core test's native capture and OCR."""

from concurrent.futures import CancelledError
from multiprocessing import get_context

from autoim.wecom.visual_capture import CaptureError
from autoim.wecom.visual_ocr import OcrUnavailable


def _read_worker(connection, calibration, thresholds):
    import ctypes

    from autoim.wecom.collection_service import CollectionReader
    from autoim.wecom.visual_capture import capture_visible_window

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    reader = CollectionReader(calibration, thresholds)
    frame = None
    with connection:
        while True:
            try:
                operation = connection.recv()
            except (EOFError, OSError):
                return
            try:
                if operation == "capture":
                    frame = result = capture_visible_window()
                elif operation in ("header", "body", "independent"):
                    result = reader.read_core(frame, lambda _: operation != "header", history_only=operation == "independent")
                else:
                    raise ValueError("unknown read operation")
                connection.send(("OK", result))
            except CaptureError as error:
                connection.send(("CAPTURE", (error.code, str(error), error.diagnostics)))
            except (EOFError, OSError):
                return
            except Exception as error:
                connection.send(("ERROR", type(error).__name__))


class CoreReadWorker:
    def __init__(self, calibration, thresholds, *, target=_read_worker):
        self.context = get_context("spawn")
        self.target = target
        self.settings = calibration, thresholds
        self.process = self.connection = None

    def _start(self):
        pipe, child = self.context.Pipe()
        process = self.context.Process(target=self.target, args=(child, *self.settings), daemon=True)
        try:
            process.start()
        except BaseException:
            pipe.close()
            raise
        finally:
            child.close()
        self.process, self.connection = process, pipe

    def request(self, operation, deadline, cancel):
        try:
            deadline()
            if cancel.is_set():
                raise CancelledError()
            if self.process is None:
                self._start()
            self.connection.send(operation)
            while not self.connection.poll(.04):
                if cancel.is_set():
                    raise CancelledError()
                deadline()
                if not self.process.is_alive():
                    raise OcrUnavailable("只读采集子进程异常退出")
            kind, result = self.connection.recv()
            deadline()
            if cancel.is_set():
                raise CancelledError()
        except (OSError, EOFError) as error:
            self.close()
            raise OcrUnavailable("只读采集子进程连接中断") from error
        except BaseException:
            self.close()
            raise
        if kind == "CAPTURE":
            code, message, diagnostics = result
            error = CaptureError(code, message)
            error.diagnostics = diagnostics
            raise error
        if kind != "OK":
            raise OcrUnavailable(f"只读采集子进程失败（{result}）")
        return result

    def close(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        if connection:
            connection.close()
        if process:
            if process.is_alive():
                process.terminate()
            process.join(.4)
            if not process.is_alive():
                process.close()
