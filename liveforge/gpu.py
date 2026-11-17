"""GPU load sampler via PDH (ctypes, no subprocesses). Windows only."""
import ctypes
import re
import sys
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x200
PDH_MORE_DATA = 0x800007D2
_ENG = r"\GPU Engine(*)\Utilization Percentage"
_MEM = r"\GPU Adapter Memory(*)\Dedicated Usage"
_RE = re.compile(r"luid_(0x\w+_0x\w+)_phys_\d+_eng_(\d+)_engtype_(.+)$", re.I)
_GFX = ("3d", "compute", "graphics", "cuda")


class _Item(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("status", wintypes.DWORD), ("value", ctypes.c_double)]


class GpuSampler:
    def __init__(self):
        self._q = None
        if sys.platform != "win32":
            return
        try:
            self._pdh = pdh = ctypes.WinDLL("pdh")
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
            pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
            pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            for f in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                      pdh.PdhCloseQuery, pdh.PdhGetFormattedCounterArrayW):
                f.restype = wintypes.DWORD
            q = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, None, ctypes.byref(q)):
                return
            self._q = q
            self._ceng, self._cmem = ctypes.c_void_p(), ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(q, _ENG, None, ctypes.byref(self._ceng)) or \
               pdh.PdhAddEnglishCounterW(q, _MEM, None, ctypes.byref(self._cmem)):
                self.close()
                return
            pdh.PdhCollectQueryData(q)  # prime rate counters
        except (OSError, AttributeError):
            self._q = None

    def _array(self, counter):
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        r = self._pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if r != PDH_MORE_DATA:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if self._pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf):
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        return [(ctypes.wstring_at(items[i].name), items[i].value) for i in range(count.value)
                if items[i].status in (0, 1)]

    def sample(self):
        if not self._q or self._pdh.PdhCollectQueryData(self._q):
            return None
        eng = {}  # (luid, eng idx, type) -> sum over processes
        for name, v in self._array(self._ceng):
            m = _RE.search(name)
            if m:
                k = (m[1], m[2], m[3].lower())
                eng[k] = eng.get(k, 0.0) + v
        ad = {}  # luid -> {"gpu","encode","decode"}: max over engine indices
        for (luid, _, t), v in eng.items():
            v = min(v, 100.0)
            kind = ("gpu" if t.startswith(_GFX) else "encode" if "encode" in t or "codec" in t
                    else "decode" if "decode" in t else None)
            if kind:
                d = ad.setdefault(luid, {"gpu": 0.0, "encode": 0.0, "decode": 0.0})
                d[kind] = max(d[kind], v)
        best = max(ad.values(), key=lambda d: sum(d.values()), default={"gpu": 0.0, "encode": 0.0, "decode": 0.0})
        vram = max((v for _, v in self._array(self._cmem)), default=0.0)
        return {"gpu_pct": best["gpu"], "encode_pct": best["encode"], "decode_pct": best["decode"],
                "vram_used_gb": vram / 1e9}

    def close(self):
        if self._q:
            self._pdh.PdhCloseQuery(self._q)
            self._q = None

    def __del__(self):
        self.close()


if __name__ == "__main__":
    import time
    s = GpuSampler()
    for _ in range(5):
        print(s.sample())
        time.sleep(1)
    s.close()
