class ProcessingError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TransientProcessingError(ProcessingError):
    pass


class ProcessingBusy(TransientProcessingError):
    pass


class LeaseLost(ProcessingError):
    pass
