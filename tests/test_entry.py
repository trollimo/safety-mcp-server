import os
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "docker", "entry.sh")


def run(tmp_path, *cmd):
    env = dict(PATH=os.environ["PATH"],
               SAFETY_KEYS_SRC=str(tmp_path / "src"), SAFETY_KEYS_DST=str(tmp_path / "dst"),
               SAFETY_KEYS_RW_SRC=str(tmp_path / "rwsrc"), SAFETY_KEYS_RW_DST=str(tmp_path / "rwdst"))
    return subprocess.run(["sh", ENTRY, *cmd], env=env, capture_output=True, text=True, timeout=10)


def test_copies_keys_with_600_and_strips_crlf(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "safety_vpn_ro").write_bytes(b"-----BEGIN-----\r\nabc\r\n-----END-----\r\n")
    (src / "safety_vpn_ro.pub").write_text("ssh-ed25519 AAAA safety vpn ro\n")
    (src / ".hidden").write_text("x")
    (src / "bad name").write_text("x")
    os.chmod(src / "safety_vpn_ro", 0o777)
    r = run(tmp_path, "echo", "started")
    assert r.returncode == 0 and r.stdout.strip() == "started"          # exec "$@"
    assert sorted(os.listdir(dst)) == ["safety_vpn_ro", "safety_vpn_ro.pub"]
    assert (dst / "safety_vpn_ro").read_bytes() == b"-----BEGIN-----\nabc\n-----END-----\n"
    for f in dst.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_noop_without_source_or_writable_destination(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "k").write_text("x")
    r = run(tmp_path, "true")                                       # dst отсутствует -> ничего не копируется
    assert r.returncode == 0 and not (tmp_path / "dst").exists()
    (tmp_path / "dst").mkdir(mode=0o500)
    try:
        assert run(tmp_path, "true").returncode == 0 and os.listdir(tmp_path / "dst") == []
    finally:
        os.chmod(tmp_path / "dst", 0o700)
