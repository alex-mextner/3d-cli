"""Unit tests for lib/profiles — repo <-> slicer preset sync and apply_values (issue #180)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from errors import GateFailure, InputNotFound, InvalidArgument, ThreeDError
from profiles import preset, slicers, sync
from profiles.values import apply_values
from profiles_fixtures import (
    K1_MACHINE,
    PLA_FILAMENT,
    U1_PROCESS,
    make_snapmaker,
    slicer_info,
    slicer_json,
)

U1_REL = f"process/{U1_PROCESS['name']}.json"
K1_REL = f"machine/{K1_MACHINE['name']}.json"
PLA_REL = "filament/base/My PLA.json"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("THREED_PROFILES_DIR", str(tmp_path / "repo" / "profiles"))
    monkeypatch.setattr(slicers, "running_processes", lambda slicer: [])
    return tmp_path


def _pair(env: Path) -> sync.Pair:
    pair = sync.find_pair(slicers.SLICERS["snapmaker-orca"])
    assert pair is not None
    return pair


def _entry(rel: str, body: str, info: str | None = None) -> sync.Entry:
    return sync.Entry(rel, body.encode(), info.encode() if info is not None else None)


# ---- three-way plan -------------------------------------------------------------------------


def test_plan_without_base_pulls_slicer_only_and_pushes_repo_only() -> None:
    repo = {"process/a.json": _entry("process/a.json", "{}\n")}
    slicer = {"process/b.json": _entry("process/b.json", "{}\n")}
    actions = {item.rel: item.action for item in sync.plan(repo, slicer, {})}
    assert actions == {"process/a.json": "push", "process/b.json": "pull"}


def test_plan_changed_on_one_side_follows_that_side() -> None:
    old = _entry("p.json", "old\n")
    new = _entry("p.json", "new\n")
    base = {"p.json": old.digest}
    assert sync.plan({"p.json": old}, {"p.json": new}, base)[0].action == "pull"
    assert sync.plan({"p.json": new}, {"p.json": old}, base)[0].action == "push"
    assert sync.plan({"p.json": new}, {"p.json": new}, base)[0].action == "same"


def test_plan_changed_on_both_sides_is_a_conflict_even_without_base() -> None:
    old, mine, theirs = (_entry("p.json", s) for s in ("old\n", "mine\n", "theirs\n"))
    assert (
        sync.plan({"p.json": mine}, {"p.json": theirs}, {"p.json": old.digest})[
            0
        ].action
        == "conflict"
    )
    assert sync.plan({"p.json": mine}, {"p.json": theirs}, {})[0].action == "conflict"


def test_plan_deletions_propagate_and_delete_vs_edit_conflicts() -> None:
    old = _entry("p.json", "old\n")
    edited = _entry("p.json", "edited\n")
    base = {"p.json": old.digest}
    gone_in_slicer = sync.plan({"p.json": old}, {}, base)[0]
    assert (gone_in_slicer.action, gone_in_slicer.slicer) == ("pull", None)
    gone_in_repo = sync.plan({}, {"p.json": old}, base)[0]
    assert (gone_in_repo.action, gone_in_repo.repo) == ("push", None)
    assert sync.plan({}, {"p.json": edited}, base)[0].action == "conflict"
    assert sync.plan({}, {}, base)[0].action == "same"


def test_info_digest_ignores_machine_local_fields() -> None:
    a = preset.portable_info(
        slicer_info(base_id="GP006", updated_time=1, user_id="1234")
    )
    b = preset.portable_info(
        slicer_info(base_id="GP006", updated_time=99, sync_info="update")
    )
    assert a == b == b"base_id = GP006\n"
    assert preset.portable_info(slicer_info()) is None


# ---- slicer discovery -----------------------------------------------------------------------


def test_user_presets_dir_follows_preset_folder(env: Path) -> None:
    make_snapmaker(env / "xdg-config", preset_folder="12345")
    pair = _pair(env)
    assert pair.user_dir == env / "xdg-config" / "Snapmaker_Orca" / "user" / "12345"
    assert pair.repo_dir == env / "repo" / "profiles" / "snapmaker-orca"


@pytest.mark.parametrize("folder", ["/tmp/other", "../../other", "..", "a/b"])
def test_preset_folder_that_leaves_the_user_dir_is_refused(
    env: Path, folder: str
) -> None:
    data = env / "xdg-config" / "Snapmaker_Orca"
    (data / "user").mkdir(parents=True)
    (data / "Snapmaker_Orca.conf").write_text(
        json.dumps({"app": {"preset_folder": folder}}), encoding="utf-8"
    )
    with pytest.raises(ThreeDError, match="is not a folder name"):
        sync.find_pair(slicers.SLICERS["snapmaker-orca"])


def test_second_sync_is_refused_while_the_lock_is_held(env: Path) -> None:
    with (
        sync.exclusive(),
        pytest.raises(GateFailure, match="another `3d profiles sync` is running"),
        sync.exclusive(),
    ):
        pass
    with sync.exclusive():  # released again
        pass


def test_flatpak_data_dir_is_found(env: Path) -> None:
    flat = (
        env
        / "home"
        / ".var"
        / "app"
        / "com.bambulab.BambuStudio"
        / "config"
        / "BambuStudio"
    )
    (flat / "user" / "default").mkdir(parents=True)
    assert slicers.find_data_dir(slicers.SLICERS["bambu"]) == flat


def test_missing_slicer_has_no_pair(env: Path) -> None:
    assert sync.find_pair(slicers.SLICERS["orca"]) is None


# ---- sync end to end on the library ---------------------------------------------------------


def test_first_sync_imports_presets_without_machine_local_keys(env: Path) -> None:
    make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    items = sync.status(pair, state)
    assert {i.rel: i.action for i in items} == {
        U1_REL: "pull",
        K1_REL: "pull",
        PLA_REL: "pull",
    }
    sync.apply(pair, items, state)
    state.save()

    repo_k1 = pair.repo_dir / K1_REL
    assert "print_host" not in json.loads(repo_k1.read_text(encoding="utf-8"))
    assert (pair.repo_dir / U1_REL).read_text(encoding="utf-8") == slicer_json(
        U1_PROCESS
    )
    assert (pair.repo_dir / U1_REL).with_suffix(".info").read_text(
        encoding="utf-8"
    ) == "base_id = GP006\n"
    assert not (pair.repo_dir / PLA_REL).with_suffix(".info").exists()
    assert all(i.action == "same" for i in sync.status(pair, sync.State.load()))


def test_push_backs_up_keeps_local_keys_and_marks_cloud_update(env: Path) -> None:
    user = make_snapmaker(env / "xdg-config")
    (user / K1_REL).with_suffix(".info").write_text(
        slicer_info(setting_id="PMUS-1", base_id="GM001", updated_time=5),
        encoding="utf-8",
    )
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)

    repo_k1 = pair.repo_dir / K1_REL
    obj = json.loads(repo_k1.read_text(encoding="utf-8"))
    obj["host_type"] = "octoprint"
    repo_k1.write_text(slicer_json(obj), encoding="utf-8")
    items = sync.status(pair, state)
    assert [i.action for i in items if i.rel == K1_REL] == ["push"]
    result = sync.apply(pair, items, state, now=1800000000)

    pushed = json.loads((user / K1_REL).read_text(encoding="utf-8"))
    assert pushed["host_type"] == "octoprint"
    assert pushed["print_host"] == "192.168.0.27"
    assert list(pushed) == sorted(pushed)
    info = (user / K1_REL).with_suffix(".info").read_text(encoding="utf-8")
    assert "sync_info = update\n" in info and "updated_time = 1800000000\n" in info
    assert "setting_id = PMUS-1\n" in info
    assert result.backup_dir is not None
    backup = json.loads(
        (result.backup_dir / "snapmaker-orca" / K1_REL).read_text(encoding="utf-8")
    )
    assert backup["host_type"] == "crealityprint"
    assert all(i.action == "same" for i in sync.status(pair, state))


def test_repo_deletion_removes_slicer_files_after_backup(env: Path) -> None:
    user = make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    (pair.repo_dir / U1_REL).unlink()
    (pair.repo_dir / U1_REL).with_suffix(".info").unlink()
    result = sync.apply(pair, sync.status(pair, state), state)
    assert not (user / U1_REL).exists()
    assert not (user / U1_REL).with_suffix(".info").exists()
    assert result.backup_dir is not None
    assert (result.backup_dir / "snapmaker-orca" / U1_REL).exists()


def test_conflict_is_reported_with_a_diff_and_never_applied(env: Path) -> None:
    user = make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    for path, height in ((pair.repo_dir / U1_REL, "0.30"), (user / U1_REL, "0.24")):
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["layer_height"] = height
        path.write_text(slicer_json(obj), encoding="utf-8")
    (conflict,) = [i for i in sync.status(pair, state) if i.action != "same"]
    assert conflict.action == "conflict"
    text = sync.item_diff(conflict, pair.slicer.id)
    assert (
        '-    "layer_height": "0.30",' in text
        and '+    "layer_height": "0.24",' in text
    )
    sync.apply(pair, [conflict], state)
    assert (
        json.loads((user / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.24"
    )
    assert (
        json.loads((pair.repo_dir / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.30"
    )


def test_push_refuses_while_the_slicer_runs(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    (pair.repo_dir / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.2"}), encoding="utf-8"
    )
    monkeypatch.setattr(slicers, "running_processes", lambda slicer: ["snapmaker-orca"])
    items = sync.status(pair, state)
    with pytest.raises(GateFailure, match="running"):
        sync.apply(pair, items, state)
    sync.apply(pair, items, state, force=True)
    assert all(i.action == "same" for i in sync.status(pair, state))


def test_pull_only_direction_leaves_pushes_pending(env: Path) -> None:
    user = make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    (pair.repo_dir / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.2"}), encoding="utf-8"
    )
    (user / PLA_REL).write_text(
        slicer_json({**PLA_FILAMENT, "pressure_advance": ["0.03"]}), encoding="utf-8"
    )
    sync.apply(pair, sync.status(pair, state), state, directions=("pull",))
    left = {i.rel: i.action for i in sync.status(pair, state) if i.action != "same"}
    assert left == {U1_REL: "push"}


def test_slicer_save_after_the_plan_is_not_overwritten(env: Path) -> None:
    user = make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    (pair.repo_dir / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.2"}), encoding="utf-8"
    )
    items = sync.status(pair, state)
    (user / U1_REL).write_text(
        slicer_json({**U1_PROCESS, "layer_height": "0.16"}), encoding="utf-8"
    )
    result = sync.apply(pair, items, state)
    assert [i.rel for i in result.stale] == [U1_REL] and not result.done
    assert (
        json.loads((user / U1_REL).read_text(encoding="utf-8"))["layer_height"]
        == "0.16"
    )
    assert [i.action for i in sync.status(pair, state) if i.rel == U1_REL] == [
        "conflict"
    ]


def test_hand_written_repo_info_compares_by_base_id_only(env: Path) -> None:
    make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    state = sync.State.load()
    sync.apply(pair, sync.status(pair, state), state)
    (pair.repo_dir / U1_REL).with_suffix(".info").write_text(
        slicer_info(base_id="GP006", updated_time=7), encoding="utf-8"
    )
    assert all(i.action == "same" for i in sync.status(pair, state))


def test_repo_file_with_slicer_local_key_is_rejected(env: Path) -> None:
    make_snapmaker(env / "xdg-config")
    pair = _pair(env)
    target = pair.repo_dir / K1_REL
    target.parent.mkdir(parents=True)
    target.write_text(slicer_json(K1_MACHINE), encoding="utf-8")
    with pytest.raises(ThreeDError, match="slicer-local keys: print_host"):
        sync.status(pair, sync.State.load())


# ---- apply_values ---------------------------------------------------------------------------


def _seed_repo_filament(env: Path, indent: int | str = 4) -> Path:
    path = env / "repo" / "profiles" / "orca" / "filament" / "My PLA.json"
    path.parent.mkdir(parents=True)
    path.write_text(slicer_json(PLA_FILAMENT, indent), encoding="utf-8")
    return path


def test_apply_values_writes_vector_values_in_slicer_format(env: Path) -> None:
    path = _seed_repo_filament(env, "\t")
    diff = apply_values(
        "orca",
        "filament",
        "My PLA",
        {"filament_flow_ratio": 0.95, "pressure_advance": "0.025"},
    )
    obj = json.loads(path.read_text(encoding="utf-8"))
    assert obj["filament_flow_ratio"] == ["0.95"]
    assert obj["pressure_advance"] == ["0.025"]
    assert path.read_text(encoding="utf-8") == slicer_json(obj, "\t")
    assert '-\t"filament_flow_ratio": [\n' not in diff
    assert '-\t\t"0.98"' in diff and '+\t\t"0.95"' in diff


def test_apply_values_inserts_new_keys_in_sorted_order(env: Path) -> None:
    path = _seed_repo_filament(env)
    apply_values(
        "orca",
        "filament",
        "My PLA",
        {"enable_pressure_advance": ["1"], "slow_down_min_speed": 20},
    )
    obj = json.loads(path.read_text(encoding="utf-8"))
    assert list(obj) == sorted(obj)
    assert (
        obj["enable_pressure_advance"] == ["1"] and obj["slow_down_min_speed"] == "20"
    )


def test_apply_values_dry_run_and_noop(env: Path) -> None:
    path = _seed_repo_filament(env)
    before = path.read_text(encoding="utf-8")
    assert apply_values(
        "orca", "filament", "My PLA", {"filament_flow_ratio": 0.9}, dry_run=True
    )
    assert path.read_text(encoding="utf-8") == before
    assert (
        apply_values("orca", "filament", "My PLA", {"filament_flow_ratio": "0.98"})
        == ""
    )


def test_apply_values_rejects_bad_targets(env: Path) -> None:
    _seed_repo_filament(env)
    with pytest.raises(InputNotFound):
        apply_values("orca", "filament", "Nope", {"pressure_advance": 0.02})
    with pytest.raises(InvalidArgument):
        apply_values("prusa", "filament", "My PLA", {"pressure_advance": 0.02})
    with pytest.raises(InvalidArgument):
        apply_values("orca", "printer", "My PLA", {"pressure_advance": 0.02})
    with pytest.raises(InvalidArgument, match="print_host"):
        apply_values("orca", "filament", "My PLA", {"print_host": "1.2.3.4"})
    with pytest.raises(InvalidArgument, match="name"):
        apply_values("orca", "filament", "My PLA", {"name": "Other"})
    victim = env / "victim.json"
    victim.write_text(slicer_json(PLA_FILAMENT), encoding="utf-8")
    for name in ("../../../../victim", "..", "base/../My PLA"):
        with pytest.raises(InvalidArgument, match="a preset name"):
            apply_values("orca", "filament", name, {"pressure_advance": 0.03})
    assert victim.read_text(encoding="utf-8") == slicer_json(PLA_FILAMENT)
