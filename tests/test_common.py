import pytest

import common as C


@pytest.mark.parametrize("name,ext", [
    ("reads_R1_001.fastq.gz", "fastq.gz"),
    ("sample.1.fq.gz", "fq.gz"),
    ("x.tar.gz", "tar.gz"),
    ("x.gz", "gz"),
    ("aln.bam", "bam"),
    ("job.log.1", "log"),
    ("README", ""),
    (".bashrc", ""),
    (".env.local", "local"),
    ("genome.FA", "fa"),
    ("reads.fastq.ora", "fastq.ora"),
    ("weird.2023-01-01 backup", ""),
    ("chr1.1", ""),
])
def test_ext_of(name, ext):
    assert C.ext_of(name) == ext


@pytest.mark.parametrize("s", ["plain", "tab\there", "new\nline", "back\\slash", "\\t literal",
                               "cr\rx", "bad\udcffbyte"])
def test_esc_roundtrip(s):
    e = C.esc(s)
    assert "\t" not in e and "\n" not in e
    assert C.unesc(e) == s


@pytest.mark.parametrize("name,sens", [
    (".env", True), (".env.prod", True), ("id_rsa", True), ("id_ed25519.pub", True),
    ("server.pem", True), ("aws_credentials.txt", True), ("my_secret.bin", True),
    ("github_token", True), (".netrc", True), (".Renviron", True),
    ("reads.fastq.gz", False), ("environment.yml", False), ("keys.tsv", False),
])
def test_sensitive_file(name, sens):
    assert C.is_sensitive_file(name) is sens


def test_sensitive_dir():
    assert C.is_sensitive_dir("/x/.ssh")
    assert C.is_sensitive_dir("/x/.globus-cli")
    assert C.is_sensitive_dir("/home/u/.config/gcloud")
    assert not C.is_sensitive_dir("/x/gcloud")
    assert not C.is_sensitive_dir("/x/data")


def test_within_and_run_dir_guard(tmp_path):
    t = str(tmp_path / "t")
    assert C.is_within(t + "/a", t) and C.is_within(t, t)
    assert not C.is_within(t + "x/a", t)
    with pytest.raises(SystemExit):
        C.check_run_dir_safe(t + "/audits/run", t, [])
    C.check_run_dir_safe(t + "/repo/audits/run", t, [t + "/repo"])   # excluded: ok


def test_out_path_blocks_escape(tmp_path):
    assert C.out_path(tmp_path, "a", "b").startswith(str(tmp_path))
    with pytest.raises(SystemExit):
        C.out_path(tmp_path, "..", "evil")


def test_parse_size():
    assert C.parse_size("1MiB") == 1 << 20
    assert C.parse_size("512k") == 512 << 10
    assert C.parse_size("4096") == 4096
