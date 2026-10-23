"""threed_vision.models contracts: registry, offline-first lookup, verification cache, import/download, atomic fetch."""

from __future__ import annotations

import hashlib
import io as _io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import models  # noqa: E402
from threed_vision.errors import BadInput, MissingDependency, VisionError  # noqa: E402

PAYLOAD = b"tiny-model-bytes" * 64


def _fake_registry(
    monkeypatch: pytest.MonkeyPatch, url: str = "https://example.invalid/m.bin"
) -> models.ModelFile:
    f = models.ModelFile(
        "m.bin", hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD), url
    )
    spec = models.ModelSpec("tiny", "test model", "MIT", "m.bin", (f,))
    monkeypatch.setattr(models, "MODELS", {"tiny": spec})
    return f


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "models"
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(d))
    monkeypatch.setattr(models, "legacy_roots", lambda: [])
    models._session.clear()
    return d


def test_registry_is_pinned_and_complete() -> None:
    assert set(models.MODELS) == {
        "sam2-tiny",
        "owlv2-base",
        "clip-b32",
        "dinov2-s",
        "u2net",
    }
    for spec in models.MODELS.values():
        assert spec.licence and spec.files
        for f in spec.files:
            assert len(f.sha256) == 64 and int(f.sha256, 16) >= 0
            assert f.size > 0 and f.url.startswith("https://")
        assert any(
            f.rel == spec.primary or f.rel.startswith(spec.primary + "/")
            for f in spec.files
        )
    # Hugging Face URLs are pinned to a commit, never to a moving branch
    for spec in models.MODELS.values():
        for f in spec.files:
            assert "/resolve/main/" not in f.url
    assert [f.rel for f in models.MODELS["clip-b32"].files] == [
        "clipb32_vision.onnx",
        "clip_text.onnx",
        "clip_tokenizer.json",
    ]
    assert models.MODELS["owlv2-base"].primary == "owlv2-base-patch16-ensemble"
    assert models.MODELS["sam2-tiny"].primary == "sam2.1_hiera_tiny.pt"


def test_models_dir_env_and_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path / "x"))
    assert models.models_dir() == tmp_path / "x"
    monkeypatch.delenv("THREED_VISION_MODELS_DIR")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert (
        models.models_dir() == Path.home() / ".cache" / "3d-cli" / "vision" / "models"
    )


def test_missing_model_names_the_pull_command(cache: Path) -> None:
    with pytest.raises(MissingDependency) as exc:
        models.model_path("sam2-tiny")
    assert exc.value.exit_code == 127
    assert "3d vision-models pull sam2-tiny" in exc.value.render()


def test_unknown_model_and_file_are_bad_input(cache: Path) -> None:
    with pytest.raises(BadInput):
        models.model_path("nope")
    with pytest.raises(BadInput):
        models.model_path("clip-b32", "nope.onnx")
    with pytest.raises(BadInput):
        models.fetch(["nope"])


def test_fetch_imports_existing_copy_by_hard_link_and_is_idempotent(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])
    monkeypatch.setattr(
        models.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("must not download when a local copy exists"),
    )
    res = models.fetch(["tiny"])
    assert res["results"]["tiny"] == {"m.bin": "imported-linked"}
    assert os.stat(cache / "m.bin").st_ino == os.stat(old / "m.bin").st_ino
    assert models.fetch(["tiny"])["results"]["tiny"] == {"m.bin": "present"}
    assert models.model_path("tiny") == cache / "m.bin"
    assert not list(cache.glob("*.part*"))


def test_fetch_falls_back_to_copy_when_link_fails(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])

    def no_link(*a: Any, **k: Any) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(models.os, "link", no_link)
    assert models.fetch(["tiny"])["results"]["tiny"] == {"m.bin": "imported-copied"}
    assert (cache / "m.bin").read_bytes() == PAYLOAD


def test_corrupt_legacy_copy_is_not_imported(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch, url="https://example.invalid/m.bin")
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(b"x" * len(PAYLOAD))  # right size, wrong content
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])
    monkeypatch.setattr(
        models, "_download", lambda f, name, dest, progress: dest.write_bytes(PAYLOAD)
    )
    assert models.fetch(["tiny"])["results"]["tiny"] == {"m.bin": "downloaded"}
    assert (cache / "m.bin").read_bytes() == PAYLOAD


class _Resp(_io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def test_download_is_verified_atomic_and_reports_progress(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **k: _Resp(PAYLOAD)
    )
    seen: list[tuple[str, str, int, int]] = []
    res = models.fetch(["tiny"], progress=lambda n, r, d, t: seen.append((n, r, d, t)))
    assert res["results"]["tiny"] == {"m.bin": "downloaded"}
    assert seen[-1] == ("tiny", "m.bin", len(PAYLOAD), len(PAYLOAD))
    assert not list(cache.glob("*.part*"))


def test_download_with_wrong_checksum_leaves_nothing(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **k: _Resp(b"y" * len(PAYLOAD))
    )
    with pytest.raises(VisionError, match="fails verification"):
        models.fetch(["tiny"])
    assert not (cache / "m.bin").exists()
    assert not list(cache.glob("*.part*"))


def test_network_failure_is_a_missing_dependency_with_a_fix(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise models.urllib.error.URLError("offline")

    monkeypatch.setattr(models.urllib.request, "urlopen", boom)
    with pytest.raises(MissingDependency) as exc:
        models.fetch(["tiny"])
    assert "THREED_VISION_IMPORT_DIRS" in exc.value.render()
    assert not list(cache.glob("*.part*"))


def test_truncated_and_corrupt_files_are_rejected_by_model_path(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    cache.mkdir(parents=True)
    (cache / "m.bin").write_bytes(PAYLOAD[:10])  # truncated
    with pytest.raises(MissingDependency, match="corrupt or truncated"):
        models.model_path("tiny")
    (cache / "m.bin").write_bytes(b"z" * len(PAYLOAD))  # right size, wrong sha
    with pytest.raises(MissingDependency, match="corrupt or truncated"):
        models.model_path("tiny")
    assert (
        models.status()["models"]["tiny"]["files"][0]["state"] == "unverified"
    )  # size ok, hash not checked by status()
    assert (
        models.status(verify=True)["models"]["tiny"]["files"][0]["state"] == "corrupt"
    )


def test_verification_is_cached_by_size_and_mtime(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    cache.mkdir(parents=True)
    (cache / "m.bin").write_bytes(PAYLOAD)
    calls = {"n": 0}
    real = models._sha256

    def counting(p: Path) -> str:
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(models, "_sha256", counting)
    models.model_path("tiny")
    assert calls["n"] == 1
    models._session.clear()  # a fresh process: only the on-disk marker remains
    models.model_path("tiny")
    assert calls["n"] == 1
    (cache / "m.bin").write_bytes(PAYLOAD)  # rewritten: mtime changes -> hashed again
    os.utime(cache / "m.bin", ns=(1, 1))
    models._session.clear()
    models.model_path("tiny")
    assert calls["n"] == 2


def test_status_shape(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_registry(monkeypatch)
    st = models.status()
    assert st["models_dir"] == str(cache)
    m = st["models"]["tiny"]
    assert m["installed"] is False and m["verified"] is False
    assert m["files"][0]["state"] == "missing"
    assert m["pull"] == "3d vision-models pull tiny"
    cache.mkdir(parents=True)
    (cache / "m.bin").write_bytes(PAYLOAD)
    models.model_path("tiny")
    m = models.status()["models"]["tiny"]
    assert (
        m["installed"]
        and m["verified"]
        and m["licence"] == "MIT"
        and m["size_bytes"] == len(PAYLOAD)
    )


def test_pull_hint_is_a_copy_pasteable_command(cache: Path) -> None:
    with pytest.raises(MissingDependency) as exc:
        models.model_path("sam2-tiny")
    assert exc.value.remediation == "3d vision-models pull sam2-tiny"
    assert models.status()["models"]["u2net"]["pull"] == "3d vision-models pull u2net"


def test_a_corrupt_legacy_copy_does_not_hide_a_good_one_in_a_later_root(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.mkdir()
    good.mkdir()
    (bad / "m.bin").write_bytes(b"x" * len(PAYLOAD))  # right size, wrong bytes
    (good / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [bad, good])
    monkeypatch.setattr(
        models.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("must import the good copy"),
    )
    assert models.fetch(["tiny"])["results"]["tiny"] == {"m.bin": "imported-linked"}
    assert (cache / "m.bin").read_bytes() == PAYLOAD


def test_import_does_not_rehash_the_placed_file(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])
    calls = {"n": 0}
    real = models._sha256

    def counting(p: Path) -> str:
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(models, "_sha256", counting)
    models.fetch(["tiny"])
    assert calls["n"] == 1  # hashed once (the source), not again after placing
    models.model_path("tiny")
    assert calls["n"] == 1


def test_import_dirs_env_replaces_the_builtin_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path / "m"))
    monkeypatch.delenv("THREED_VISION_IMPORT_DIRS", raising=False)
    builtin = models.legacy_roots()
    assert tmp_path / "m" / "research" in builtin and any(
        "hsw-editor" in str(p) for p in builtin
    )
    monkeypatch.setenv(
        "THREED_VISION_IMPORT_DIRS", f"{tmp_path / 'a'}{os.pathsep}{tmp_path / 'b'}"
    )
    assert models.legacy_roots() == [tmp_path / "a", tmp_path / "b"]
    monkeypatch.setenv("THREED_VISION_IMPORT_DIRS", "")
    assert models.legacy_roots() == []  # set-but-empty disables importing


def test_default_dir_follows_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("THREED_VISION_MODELS_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert models.models_dir() == tmp_path / "xdg" / "3d-cli" / "vision" / "models"
    assert (
        models.legacy_roots()[0]
        == tmp_path / "xdg" / "3d-cli" / "vision" / "models" / "research"
    )


def test_failed_copy_cleans_up_and_is_a_structured_error(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])
    monkeypatch.setattr(
        models.os,
        "link",
        lambda *a, **k: (_ for _ in ()).throw(OSError("cross-device")),
    )

    def full_disk(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(PAYLOAD[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(models.shutil, "copyfile", full_disk)
    with pytest.raises(VisionError, match="No space left") as exc:
        models.fetch(["tiny"])
    assert not isinstance(exc.value, MissingDependency)
    assert not list(cache.glob("*.part*")) and not (cache / "m.bin").exists()


def test_disk_errors_while_downloading_are_not_blamed_on_the_network(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)

    monkeypatch.setattr(
        models.urllib.request, "urlopen", lambda *a, **k: _Resp(PAYLOAD)
    )
    real_open = open

    def failing_open(path: Any, mode: str = "r", *a: Any, **k: Any) -> Any:
        if str(path).endswith(f".part{os.getpid()}") and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", failing_open)
    with pytest.raises(VisionError, match="No space left") as exc:
        models.fetch(["tiny"])
    assert (
        not isinstance(exc.value, MissingDependency)
        and "network" not in exc.value.render()
    )


def test_dropped_connections_are_missing_dependency_not_a_traceback(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)

    class Cut(_Resp):
        def read(self, n: int | None = -1) -> bytes:
            raise models.http.client.IncompleteRead(b"abc", 100)

    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: Cut(PAYLOAD))
    with pytest.raises(MissingDependency):
        models.fetch(["tiny"])
    short = _Resp(
        PAYLOAD[: len(PAYLOAD) // 2]
    )  # a clean EOF before Content-Length bytes arrived
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: short)
    with pytest.raises(MissingDependency, match="cut short"):
        models.fetch(["tiny"])
    assert not list(cache.glob("*.part*"))


def test_fetch_sweeps_part_files_left_by_killed_pulls(
    cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_registry(monkeypatch)
    cache.mkdir(parents=True)
    stale = cache / "m.bin.part99999"
    stale.write_bytes(b"half")
    keep = cache / "notes.partial"
    keep.write_bytes(b"x")
    old = tmp_path / "old"
    old.mkdir()
    (old / "m.bin").write_bytes(PAYLOAD)
    monkeypatch.setattr(models, "legacy_roots", lambda: [old])
    models.fetch(["tiny"])
    assert not stale.exists() and keep.exists()


def test_model_path_rel_must_be_unambiguous(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f1 = models.ModelFile(
        "a/x.bin",
        hashlib.sha256(PAYLOAD).hexdigest(),
        len(PAYLOAD),
        "https://example.invalid/1",
    )
    f2 = models.ModelFile(
        "b/x.bin",
        hashlib.sha256(PAYLOAD).hexdigest(),
        len(PAYLOAD),
        "https://example.invalid/2",
    )
    monkeypatch.setattr(
        models,
        "MODELS",
        {"two": models.ModelSpec("two", "d", "MIT", "a/x.bin", (f1, f2))},
    )
    for rel in ("a/x.bin", "b/x.bin"):
        (cache / rel).parent.mkdir(parents=True, exist_ok=True)
        (cache / rel).write_bytes(PAYLOAD)
    assert models.model_path("two", "b/x.bin") == cache / "b" / "x.bin"
    with pytest.raises(BadInput, match="unambiguous"):
        models.model_path("two", "x.bin")


def test_concurrent_verifiers_do_not_lose_marker_entries(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    cache.mkdir(parents=True)
    files = [
        models.ModelFile(f"f{i}.bin", "0" * 64, 1, "https://example.invalid/x")
        for i in range(12)
    ]
    for f in files:
        (cache / f.rel).write_bytes(b"1")
    threads = [
        threading.Thread(target=models._remember, args=(f, (cache / f.rel).stat()))
        for f in files
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(models._read_marker()) == {f.rel for f in files}


def test_importing_the_registry_does_not_need_fcntl() -> None:
    import subprocess

    code = (
        "import sys; sys.modules['fcntl'] = None; "
        f"sys.path.insert(0, {str(ROOT / 'packages' / 'threed-vision' / 'src')!r}); "
        "import threed_vision.models as m; print(m.model_path.__name__)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    assert out.strip() == "model_path"
