from enum import Enum


class Mode(Enum):
    CVE = "cve"
    PATCH = "patch"


class Language(Enum):
    JAVA = "javasrc"
    C = "newc"
    CPP = "newcpp"


class HunkType(Enum):
    ADD = "add"
    DEL = "del"
    MOD = "mod"


class ErrorCode(Enum):
    SUCCESS = "SUCCESS"
    EXCEPTION = "EXCEPTION"
    JOERN_ERROR = "JOERN_ERROR"
    AST_ERROR = "AST_ERROR"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    REFERENCE_METHOD_MISMATCH = "REFERENCE_METHOD_MISMATCH"
    TARGET_METHOD_NOT_FOUND = "TARGET_METHOD_NOT_FOUND"
    AMBIGUOUS_METHOD = "AMBIGUOUS_METHOD"
    CHANGE_OUTSIDE_METHOD = "CHANGE_OUTSIDE_METHOD"
    PARTIAL_BACKPORT_FAILED = "PARTIAL_BACKPORT_FAILED"
    INVALID_PATCH = "INVALID_PATCH"
    PDG_NOT_FOUND = "PDG_NOT_FOUND"
    SLICE_FAILED = "SLICE_FAILED"
    GROUNDTRUTH_SLICE_FAILED = "GROUNDTRUTH_FAILED"


class BPType(Enum):
    SAME = "SAME"
    DIFF = "DIFF"


class SliceType(Enum):
    SAME = "SAME"
    DIFF = "DIFF"


class Metric(Enum):
    EM = "EM"
    NOT_EM = "NOT-EM"
