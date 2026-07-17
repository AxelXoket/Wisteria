"""Sanitize artimli filtre: '< think>' sizintisi kapali, monoton emit, eski
davranisla gorunur-metin denkligi, O(n) performans.

Denetim O11 regresyonlari (sizinti vakasi canli probe ile kanitlanmisti).
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.sanitize import StreamFilter, _visible, final_clean


def run(chunks):
    f = StreamFilter()
    out = "".join(f.feed(c) for c in chunks)
    out += f.flush_tail()
    return out, f


def test_1_spaced_tag_leak_fixed():
    out, f = run(["hello < thi", "nk>SECRET</think>", " world"])
    assert "SECRET" not in out, "gizli blok icerigi SIZDI"
    assert "< thi" not in out, "etiket kirintisi SIZDI (eski hata birebir)"
    assert out == "hello  world", out
    assert out == _visible(f.raw), "akis toplami tam-metin gorunurluguyle ayni olmali"
    print("1) '< think>' sizintisi kapali + tam-metin denkligi OK")


def test_2_basic_equivalence_corpus():
    cases = [
        ["a<think>x", "yz</think>b"],
        ["<observe>gizli ", "inceleme</observe>gorunur yanit"],
        ["foo<th", "ink>hidden</thi", "nk>bar"],
        ["dusunmeden ", "duz yanit."],
        ["3<5 ve <b", "old> metin kaliyor"],
        ["parcali <OBSERVE>BUYUK</OBSERVE>harf"],
        ["cok<think>a</think>katmanli<observe>b</observe>ornek"],
        ["yildizli *aksiyon* ve <thinking>ic ses</thinking> devami"],
    ]
    for chunks in cases:
        out, f = run(chunks)
        assert out == _visible(f.raw), f"denklik bozuldu: {chunks!r} -> {out!r}"
    print(f"2) {len(cases)} vakalik korpus: eski gorunurlukle birebir OK")


def test_3_unclosed_and_partials():
    out, _ = run(["merhaba<think>bitmedi hic"])
    assert out == "merhaba", "kapanmamis gizli blok sonsuza dek gizli kalmali"
    out, _ = run(["metin <thi"])
    assert out == "metin <thi", "etikete donusmeyen kuyruk flush'ta gorunur olmali"
    out, _ = run(["x<|channel|>thought gizli sey<|end|>y"])
    assert "gizli sey" not in out and out.startswith("x") and out.endswith("y"), out
    print("3) kapanmamis blok + kuyruk flush + channel OK")


def test_4_monotonic_emit():
    f = StreamFilter()
    total = ""
    for c in ["ab< t", "hink>zzz</think>", "cd", "<obse", "rvation>q</observation>", "ef"]:
        d = f.feed(c)
        total += d
    total += f.flush_tail()
    assert total == "abcdef", total
    print("4) monoton emit (geri alma yok) OK")


def test_5_performance_linear():
    f = StreamFilter()
    chunk = "kelime " * 5
    t0 = time.perf_counter()
    for _ in range(2000):                       # ~70KB, 2000 parca
        f.feed(chunk)
    f.flush_tail()
    stream_s = time.perf_counter() - t0
    assert stream_s < 0.5, f"akis filtresi lineer olmali: {stream_s:.2f}s"

    junk = ("ha " * 20000) + "X"                # 60KB near-miss kuyruk
    t0 = time.perf_counter()
    final_clean(junk)
    clean_s = time.perf_counter() - t0
    assert clean_s < 0.3, f"final_clean kuyrugu sinirli olmali: {clean_s:.2f}s (eski: 6.6s)"
    print(f"5) performans: akis {stream_s*1000:.0f}ms / final {clean_s*1000:.0f}ms OK")


def test_6_final_clean_behavior_kept():
    assert final_clean("dur dur dur dur dur dur") == "dur"
    assert final_clean("cok guzel!!!!!!!") == "cok guzel!!!"
    assert final_clean("a ----------- b") == "a -- b"
    assert final_clean("x<think>gizli</think>y") == "xy"
    assert final_clean("kalinti <|channel|> temiz") == "kalinti  temiz"
    print("6) final_clean davranis korumasi OK")


test_1_spaced_tag_leak_fixed()
test_2_basic_equivalence_corpus()
test_3_unclosed_and_partials()
test_4_monotonic_emit()
test_5_performance_linear()
test_6_final_clean_behavior_kept()
print("SANITIZE TAMAM")
