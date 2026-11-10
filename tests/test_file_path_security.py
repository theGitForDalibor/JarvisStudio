"""Comprehensive security tests for workspace filesystem path resolution and isolation."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis_studio.tools.builtin.file_tools import (
    list_directory,
    read_file,
    resolve_safe_path,
    write_file,
)


@pytest.fixture
def test_workspace(tmp_path):
    """Create a temporary dedicated workspace directory."""
    ws = tmp_path / "isolated_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def test_01_normal_workspace_read_write(test_workspace):
    """Test 1: Normal file read and write within the workspace succeed."""
    # Direct safe path resolution
    resolved = resolve_safe_path("subdir/doc.txt", workspace_root=test_workspace)
    assert resolved.is_relative_to(test_workspace)

    # Use tools with patched settings
    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)

        # Write
        write_res = write_file.invoke(
            {"file_path": "notes.txt", "content": "Jarvis Secure Note"}
        )
        assert "Successfully wrote" in write_res

        # Read
        read_res = read_file.invoke({"file_path": "notes.txt"})
        assert read_res == "Jarvis Secure Note"

        # List
        list_res = list_directory.invoke({"directory_path": "."})
        assert "notes.txt" in list_res


def test_02_relative_path_traversal_escape(test_workspace):
    """Test 2: Relative path traversal attempts (../../) are strictly rejected."""
    # Direct function check
    with pytest.raises(PermissionError, match="escapes workspace root"):
        resolve_safe_path("../../passwords.txt", workspace_root=test_workspace)

    with pytest.raises(PermissionError, match="escapes workspace root"):
        resolve_safe_path(
            "subfolder/../../../escape.txt", workspace_root=test_workspace
        )

    # Tool invocation check
    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)

        res = read_file.invoke({"file_path": "../../secret.conf"})
        assert "Error reading file" in res
        assert "escapes workspace root" in res


def test_03_root_and_system_directory_escape(test_workspace):
    """Test 3: Absolute paths pointing to OS system roots are blocked."""
    if sys.platform == "win32":
        # Windows system path
        with pytest.raises(PermissionError, match="escapes workspace root"):
            resolve_safe_path(
                r"C:\Windows\System32\cmd.exe", workspace_root=test_workspace
            )
    else:
        # POSIX native system root paths
        for posix_system_path in (
            "/",
            "/etc/shadow",
            "/usr/bin/env",
            "/var/log/syslog",
        ):
            with pytest.raises(PermissionError, match="escapes workspace root"):
                resolve_safe_path(posix_system_path, workspace_root=test_workspace)

    # Unix root path (also resolves outside workspace drive root on Windows)
    with pytest.raises(PermissionError, match="escapes workspace root"):
        resolve_safe_path("/etc/passwd", workspace_root=test_workspace)

    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)
        res = read_file.invoke({"file_path": "/etc/passwd"})
        assert "Error reading file" in res
        assert "escapes workspace root" in res


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows drive-letter semantics (C:, D:, E:) apply only on win32",
)
def test_04_windows_drive_jumping_escape():
    """Test 4: Windows drive jumping (e.g. workspace on D:, path on C:) is blocked."""
    mock_d_workspace = Path(r"D:\myProject\JarvisStudio\workspace")

    # Target path on C:
    with pytest.raises(PermissionError, match="escapes workspace root"):
        resolve_safe_path(
            r"C:\Users\target.txt", workspace_root=mock_d_workspace
        )

    # Target path on E:
    with pytest.raises(PermissionError, match="escapes workspace root"):
        resolve_safe_path(
            r"E:\Confidential\file.txt", workspace_root=mock_d_workspace
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX cross-directory and root-tree escape semantics apply only on non-Windows",
)
def test_04b_posix_cross_directory_and_root_escape(test_workspace, tmp_path):
    """Test 4b (POSIX): Absolute paths outside workspace or in sibling trees are blocked."""
    sibling_workspace = tmp_path / "other_workspace"
    sibling_workspace.mkdir(parents=True, exist_ok=True)
    sibling_target = sibling_workspace / "secret.txt"

    for outside_path in (
        str(sibling_target),
        str(tmp_path),
        "/tmp/jarvis_outside_probe.txt",
        "/root/.ssh/id_rsa",
    ):
        with pytest.raises(PermissionError, match="escapes workspace root"):
            resolve_safe_path(outside_path, workspace_root=test_workspace)

    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)
        res = write_file.invoke(
            {"file_path": str(sibling_target), "content": "escaped"}
        )
        assert "Error writing file" in res
        assert "escapes workspace root" in res
        assert not sibling_target.exists()


def test_05_unc_path_escape(test_workspace):
    """Test 5: Windows UNC network share paths are strictly rejected."""
    with pytest.raises(PermissionError, match="UNC paths are not permitted"):
        resolve_safe_path(r"\\evil-share\loot", workspace_root=test_workspace)

    with pytest.raises(PermissionError, match="UNC paths are not permitted"):
        resolve_safe_path("//evil-server/data", workspace_root=test_workspace)


def test_06_symlink_reading_outside_workspace(test_workspace, tmp_path):
    """Test 6: Reading a symlink that points outside the workspace is blocked."""
    # Create external target file
    outside_dir = tmp_path / "outside_zone"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "secret_doc.txt"
    outside_file.write_text("classified data", encoding="utf-8")

    # Attempt symlink creation
    symlink_path = test_workspace / "symlink_to_outside"
    try:
        os.symlink(str(outside_file), str(symlink_path))
    except (OSError, NotImplementedError):
        # On Windows without developer mode/admin rights, mock realpath behavior
        orig_realpath = os.path.realpath

        def mock_realpath(path, *args, **kwargs):
            if "symlink_to_outside" in str(path):
                return str(outside_file)
            return orig_realpath(path, *args, **kwargs)

        with patch("os.path.realpath", side_effect=mock_realpath), pytest.raises(
            PermissionError, match=r"(outside|escapes) workspace root"
        ):
            resolve_safe_path(
                "symlink_to_outside", workspace_root=test_workspace
            )
        return

    # If symlink was created successfully:
    with pytest.raises(
        PermissionError, match=r"(outside|escapes) workspace root"
    ):
        resolve_safe_path("symlink_to_outside", workspace_root=test_workspace)

    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)
        read_res = read_file.invoke({"file_path": "symlink_to_outside"})
        assert "Error reading file" in read_res
        assert (
            "outside workspace root" in read_res
            or "escapes workspace root" in read_res
        )


def test_07_symlink_writing_outside_workspace(test_workspace, tmp_path):
    """Test 7: Writing through a symlink that points outside the workspace is blocked."""
    outside_dir = tmp_path / "outside_zone"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_target = outside_dir / "victim.txt"

    symlink_path = test_workspace / "symlink_writer"
    try:
        os.symlink(str(outside_target), str(symlink_path))
    except (OSError, NotImplementedError):
        orig_realpath = os.path.realpath

        def mock_realpath(path, *args, **kwargs):
            if "symlink_writer" in str(path):
                return str(outside_target)
            return orig_realpath(path, *args, **kwargs)

        with patch("os.path.realpath", side_effect=mock_realpath), pytest.raises(
            PermissionError, match=r"(outside|escapes) workspace root"
        ):
            resolve_safe_path("symlink_writer", workspace_root=test_workspace)
        return

    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)
        write_res = write_file.invoke(
            {"file_path": "symlink_writer", "content": "overwritten data"}
        )
        assert "Error writing file" in write_res
        assert (
            "outside workspace root" in write_res
            or "escapes workspace root" in write_res
        )


def test_08_circular_symlinks(test_workspace):
    """Test 8: Circular or recursive symlink loops are safely detected and rejected."""
    orig_realpath = os.path.realpath

    def mock_realpath(path, *args, **kwargs):
        if "loop_link" in str(path):
            raise OSError(
                "[Errno 40] Too many levels of symbolic links: 'loop_link'"
            )
        return orig_realpath(path, *args, **kwargs)

    with patch("os.path.realpath", side_effect=mock_realpath), pytest.raises(
        PermissionError, match="Symlink loop or unresolvable path"
    ):
        resolve_safe_path("loop_link", workspace_root=test_workspace)


def test_09_special_characters_and_invalid_colons(test_workspace):
    """Test 9: Paths containing null bytes, invalid colons (NTFS streams), or empty values fail."""
    # Null byte injection
    with pytest.raises(ValueError, match="null byte"):
        resolve_safe_path("file\x00.txt", workspace_root=test_workspace)

    # NTFS alternate data stream (ADS) injection
    with pytest.raises(ValueError, match="Invalid colon"):
        resolve_safe_path("file.txt:hidden_stream", workspace_root=test_workspace)

    # Empty path
    with pytest.raises(ValueError, match="cannot be empty"):
        resolve_safe_path("   ", workspace_root=test_workspace)

    # None target
    with pytest.raises(ValueError, match="cannot be None"):
        resolve_safe_path(None, workspace_root=test_workspace)


def test_10_nested_parent_directories_safe_creation(test_workspace):
    """Test 10: Non-existent deep parent directories are safely created within workspace."""
    nested_rel = "deep/nested/sub/level/document.txt"
    resolved = resolve_safe_path(nested_rel, workspace_root=test_workspace)
    assert resolved.is_relative_to(test_workspace)

    with patch(
        "jarvis_studio.tools.builtin.file_tools.get_settings"
    ) as mock_settings:
        mock_settings.return_value.storage.workspace_root = str(test_workspace)

        # Write to deep path creates all missing parents safely
        res = write_file.invoke(
            {"file_path": nested_rel, "content": "Deep nested content"}
        )
        assert "Successfully wrote" in res
        assert (test_workspace / "deep" / "nested" / "sub" / "level").is_dir()
        assert (test_workspace / nested_rel).read_text(
            encoding="utf-8"
        ) == "Deep nested content"

        # Traversal embedded in nested path fails
        escape_nested = "deep/nested/../../../../outside_jail.txt"
        with pytest.raises(PermissionError, match="escapes workspace root"):
            resolve_safe_path(escape_nested, workspace_root=test_workspace)
