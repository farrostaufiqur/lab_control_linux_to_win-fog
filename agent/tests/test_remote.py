import sys

from labagent import remote


def job(script, shell="cmd", timeout=10, job_id=7):
    return {"id": job_id, "shell": shell, "script": script, "timeout": timeout}


def run_python(shell, path):
    """Pengganti windows_command di Linux: skrip berisi kode Python."""
    return [sys.executable, str(path)], b""


def test_valid_job_rules():
    assert remote.valid_job(job("dir"))
    for bad in (None, "dir", {**job("dir"), "shell": "bash"}, job(""), job("x" * 4001), job("dir", timeout=0),
                job("dir", timeout=901), {**job("dir"), "id": "7"}, {"id": 1, "shell": "cmd", "script": "x"}):
        assert not remote.valid_job(bad)


def test_runs_script_and_captures_output_and_cleans_up(tmp_path):
    code, output, timed_out = remote.run_script(job("print('halo dunia')\nraise SystemExit(3)"), tmp_path, build=run_python)
    assert (code, timed_out) == (3, False) and output.strip() == "halo dunia"
    assert list(tmp_path.iterdir()) == []


def test_stderr_is_merged(tmp_path):
    code, output, _ = remote.run_script(job("import sys; sys.stderr.write('galat')"), tmp_path, build=run_python)
    assert code == 0 and "galat" in output


def test_timeout_kills_the_process(tmp_path):
    code, output, timed_out = remote.run_script(job("import time\ntime.sleep(60)", timeout=1), tmp_path, build=run_python)
    assert timed_out and code != 0


def test_huge_output_keeps_only_the_tail(tmp_path):
    body = "print('a' * 200000)\nprint('AKHIR')"
    _, output, _ = remote.run_script(job(body), tmp_path, build=run_python)
    assert len(output) <= remote.OUTPUT_CHARS + 20 and output.rstrip().endswith("AKHIR") and output.startswith("…(dipotong)")


def test_start_failure_is_reported(tmp_path):
    code, output, timed_out = remote.run_script(job("x"), tmp_path, build=lambda shell, path: (["/tidak/ada/exe"], b""))
    assert code == remote.FAILED_TO_START and "gagal memulai" in output and not timed_out


def test_windows_command_lines(tmp_path):
    line, header = remote.windows_command("cmd", tmp_path / "job-1.cmd")
    assert line.startswith('cmd.exe /d /s /c ""') and b"chcp 65001" in header
    line, header = remote.windows_command("powershell", tmp_path / "job-1.ps1")
    assert "-ExecutionPolicy Bypass -File" in line and header.startswith(b"\xef\xbb\xbf")
