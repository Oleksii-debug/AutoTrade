"""Fail-closed Windows Authenticode policy and verification for AutoTrade releases.

The signing backend is configuration, not release authority. A successful code
signature never grants trading authority or release qualification; callers must
still satisfy the canonical WP-64, clean-Windows, recovery and accessibility
gates for the exact delivered artifact digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
AUTHENTICODE_POLICY_PATH = ROOT / "packaging" / "windows" / "authenticode-policy.json"
MAIN_EXE = "AutoTrade.Desktop.exe"
UPDATE_EXE = "Update.exe"
CANONICAL_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
THUMBPRINT = re.compile(r"^[0-9A-F]{40}$")
MAX_VERIFIED_PE_BYTES = 512 * 1024 * 1024
POLICY_FIELDS = {
    "schema_version",
    "enabled",
    "backend",
    "endpoint",
    "code_signing_account_name",
    "certificate_profile_name",
    "timestamp_required",
}


class AuthenticodeSigningError(ValueError):
    """Raised when signing configuration or delivered signatures are not trusted."""


@dataclass(frozen=True)
class AuthenticodePolicy:
    enabled: bool
    backend: str
    endpoint: str | None
    code_signing_account_name: str | None
    certificate_profile_name: str | None
    timestamp_required: bool

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise AuthenticodeSigningError("Authenticode policy enabled must be boolean")
        if self.backend != "AZURE_ARTIFACT_SIGNING":
            raise AuthenticodeSigningError(
                "Authenticode policy backend must be AZURE_ARTIFACT_SIGNING"
            )
        if type(self.timestamp_required) is not bool or not self.timestamp_required:
            raise AuthenticodeSigningError(
                "production Authenticode policy must require a timestamp"
            )
        values = (
            self.endpoint,
            self.code_signing_account_name,
            self.certificate_profile_name,
        )
        if not self.enabled:
            if any(value is not None for value in values):
                raise AuthenticodeSigningError(
                    "disabled Authenticode policy cannot select a signer profile"
                )
            return
        if not all(isinstance(value, str) and value for value in values):
            raise AuthenticodeSigningError(
                "enabled Authenticode policy requires endpoint/account/profile"
            )
        assert self.endpoint is not None
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme != "https"
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
            or endpoint.hostname is None
            or not endpoint.hostname.lower().endswith(".codesigning.azure.net")
        ):
            raise AuthenticodeSigningError(
                "Authenticode policy endpoint must be a canonical Azure signing HTTPS endpoint"
            )
        for name, value in (
            ("code signing account", self.code_signing_account_name),
            ("certificate profile", self.certificate_profile_name),
        ):
            assert isinstance(value, str)
            if TOKEN.fullmatch(value) is None:
                raise AuthenticodeSigningError(
                    f"Authenticode policy {name} is not canonical"
                )

    def azure_metadata(self) -> dict[str, str]:
        if not self.enabled:
            raise AuthenticodeSigningError("Authenticode signing policy is disabled")
        assert self.endpoint is not None
        assert self.code_signing_account_name is not None
        assert self.certificate_profile_name is not None
        return {
            "Endpoint": self.endpoint,
            "CodeSigningAccountName": self.code_signing_account_name,
            "CertificateProfileName": self.certificate_profile_name,
        }


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AuthenticodeSigningError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _stable_regular_bytes(path: Path, *, name: str, maximum: int = 1024 * 1024) -> bytes:
    try:
        before_path = os.lstat(path)
    except OSError as error:
        raise AuthenticodeSigningError(f"{name} is unavailable") from error
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_nlink != 1:
        raise AuthenticodeSigningError(f"{name} must be one regular non-hardlinked file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuthenticodeSigningError(f"{name} could not be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino)
        ):
            raise AuthenticodeSigningError(f"{name} identity changed during admission")
        if before.st_size > maximum:
            raise AuthenticodeSigningError(f"{name} exceeds the bounded policy size")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(maximum + 1)
        if len(payload) > maximum:
            raise AuthenticodeSigningError(f"{name} exceeds the bounded policy size")
        after = os.fstat(descriptor)
        try:
            after_path = os.lstat(path)
        except OSError as error:
            raise AuthenticodeSigningError(f"{name} path changed during read") from error
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after) or identity(after) != identity(after_path):
            raise AuthenticodeSigningError(f"{name} changed during read")
        return payload
    finally:
        os.close(descriptor)


def load_canonical_authenticode_policy() -> tuple[AuthenticodePolicy, str]:
    payload = _stable_regular_bytes(
        AUTHENTICODE_POLICY_PATH,
        name="canonical Authenticode policy",
    )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except UnicodeDecodeError as error:
        raise AuthenticodeSigningError(
            "canonical Authenticode policy must be UTF-8"
        ) from error
    except json.JSONDecodeError as error:
        raise AuthenticodeSigningError(
            "canonical Authenticode policy is invalid JSON"
        ) from error
    if not isinstance(value, dict) or set(value) != POLICY_FIELDS:
        raise AuthenticodeSigningError(
            "canonical Authenticode policy fields do not match supported schema"
        )
    if value["schema_version"] != "1.0.0":
        raise AuthenticodeSigningError("unsupported Authenticode policy schema")
    policy = AuthenticodePolicy(
        enabled=value["enabled"],
        backend=value["backend"],
        endpoint=value["endpoint"],
        code_signing_account_name=value["code_signing_account_name"],
        certificate_profile_name=value["certificate_profile_name"],
        timestamp_required=value["timestamp_required"],
    )
    return policy, "sha256:" + sha256(payload).hexdigest()


def azure_metadata_bytes(policy: AuthenticodePolicy) -> bytes:
    return (
        json.dumps(
            policy.azure_metadata(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def assert_windows_signing_environment() -> None:
    if os.name != "nt":
        raise AuthenticodeSigningError(
            "Azure Artifact Signing through Velopack requires a Windows signing worker"
        )


def _trusted_windows_powershell() -> Path:
    assert_windows_signing_environment()
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_windows_directory = kernel32.GetWindowsDirectoryW
    get_windows_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    get_windows_directory.restype = ctypes.c_uint
    length = get_windows_directory(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise AuthenticodeSigningError("Windows directory could not be resolved")
    windows = Path(buffer.value).resolve(strict=True)
    candidate = (
        windows
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    ).resolve(strict=True)
    try:
        candidate.relative_to(windows)
    except ValueError as error:
        raise AuthenticodeSigningError(
            "trusted Windows PowerShell path escaped the Windows directory"
        ) from error
    if not candidate.is_file():
        raise AuthenticodeSigningError("trusted Windows PowerShell is unavailable")
    return candidate


def _powershell_environment() -> dict[str, str]:
    result = {}
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            result[key] = value
    return result


def _verify_authenticode_file(path: Path, *, runner=subprocess.run) -> dict[str, str]:
    powershell = _trusted_windows_powershell()
    script = r"""
& {
  param([string]$TargetPath)
  $ErrorActionPreference = 'Stop'
  $s = Get-AuthenticodeSignature -LiteralPath $TargetPath
  $result = [ordered]@{
    status = $s.Status.ToString()
    signer_thumbprint = if ($null -ne $s.SignerCertificate) { $s.SignerCertificate.Thumbprint } else { $null }
    signer_subject = if ($null -ne $s.SignerCertificate) { $s.SignerCertificate.Subject } else { $null }
    timestamp_thumbprint = if ($null -ne $s.TimeStamperCertificate) { $s.TimeStamperCertificate.Thumbprint } else { $null }
  }
  $result | ConvertTo-Json -Compress
}
""".strip()
    try:
        completed = runner(
            [
                os.fspath(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                os.fspath(path),
            ],
            env=_powershell_environment(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AuthenticodeSigningError(
            "Authenticode verification process failed closed"
        ) from error
    if completed.returncode != 0:
        raise AuthenticodeSigningError(
            f"Authenticode verification failed with exit code {completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as error:
        raise AuthenticodeSigningError(
            "Authenticode verifier returned invalid JSON"
        ) from error
    if not isinstance(value, dict) or set(value) != {
        "status",
        "signer_thumbprint",
        "signer_subject",
        "timestamp_thumbprint",
    }:
        raise AuthenticodeSigningError(
            "Authenticode verifier returned an unexpected result schema"
        )
    if value["status"] != "Valid":
        raise AuthenticodeSigningError(
            f"Authenticode signature is not valid: {value['status']}"
        )
    thumbprint = value["signer_thumbprint"]
    subject = value["signer_subject"]
    timestamp = value["timestamp_thumbprint"]
    if not isinstance(thumbprint, str):
        raise AuthenticodeSigningError("Authenticode signer certificate is missing")
    thumbprint = thumbprint.upper()
    if THUMBPRINT.fullmatch(thumbprint) is None:
        raise AuthenticodeSigningError("Authenticode signer thumbprint is not canonical")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticodeSigningError("Authenticode signer subject is missing")
    if not isinstance(timestamp, str):
        raise AuthenticodeSigningError("Authenticode timestamp certificate is missing")
    timestamp = timestamp.upper()
    if THUMBPRINT.fullmatch(timestamp) is None:
        raise AuthenticodeSigningError("Authenticode timestamp thumbprint is not canonical")
    return {
        "signer_thumbprint": thumbprint,
        "signer_subject": subject,
        "timestamp_thumbprint": timestamp,
    }


def _safe_package_entry(name: str) -> PurePosixPath:
    pure = PurePosixPath(name)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in name
    ):
        raise AuthenticodeSigningError("signed package contains an unsafe path")
    return pure


def _snapshot_file_to_private_copy(
    source: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
) -> None:
    if CANONICAL_SHA256.fullmatch(expected_digest) is None:
        raise AuthenticodeSigningError("signed artifact digest is not canonical")
    try:
        before_path = os.lstat(source)
    except OSError as error:
        raise AuthenticodeSigningError("signed artifact is unavailable") from error
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_nlink != 1:
        raise AuthenticodeSigningError("signed artifact must be one regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise AuthenticodeSigningError(
            "signed artifact could not be opened safely"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino)
            or before.st_size != expected_size
        ):
            raise AuthenticodeSigningError("signed artifact identity changed")
        digest = sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream, destination.open("xb") as target:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(descriptor)
        try:
            after_path = os.lstat(source)
        except OSError as error:
            raise AuthenticodeSigningError(
                "signed artifact path changed during verification"
            ) from error
        if after.st_nlink != 1 or after_path.st_nlink != 1:
            raise AuthenticodeSigningError(
                "signed artifact gained a hard-link alias during verification"
            )
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if (
            identity(before) != identity(after)
            or identity(after) != identity(after_path)
            or size != expected_size
            or "sha256:" + digest.hexdigest() != expected_digest
        ):
            raise AuthenticodeSigningError("signed artifact changed during verification snapshot")
    finally:
        os.close(descriptor)


def verify_velopack_authenticode(
    generated: list[tuple[Path, str, int]],
    *,
    version: str,
    policy: AuthenticodePolicy,
    policy_sha256: str,
) -> dict[str, object]:
    """Verify Setup plus canonical app/update executables from the signed package."""

    assert_windows_signing_environment()
    if not policy.enabled:
        raise AuthenticodeSigningError("Authenticode signing policy is disabled")
    if CANONICAL_SHA256.fullmatch(policy_sha256) is None:
        raise AuthenticodeSigningError("Authenticode policy digest is not canonical")

    by_name = {path.name: (path, digest, size) for path, digest, size in generated}
    setup_name = "AutoTrade-Setup.exe"
    package_name = f"AutoTrade-{version}-full.nupkg"
    if setup_name not in by_name or package_name not in by_name:
        raise AuthenticodeSigningError(
            "signed Velopack output is missing Setup or exact full package"
        )

    records: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="autotrade-authenticode-verify-") as raw:
        root = Path(raw)
        setup_source, setup_digest, setup_size = by_name[setup_name]
        setup_copy = root / setup_name
        _snapshot_file_to_private_copy(
            setup_source,
            setup_copy,
            expected_digest=setup_digest,
            expected_size=setup_size,
        )
        setup_signature = _verify_authenticode_file(setup_copy)
        records.append(
            {
                "path": setup_name,
                "sha256": setup_digest,
                "size": setup_size,
                **setup_signature,
            }
        )

        package_source, package_digest, package_size = by_name[package_name]
        package_copy = root / package_name
        _snapshot_file_to_private_copy(
            package_source,
            package_copy,
            expected_digest=package_digest,
            expected_size=package_size,
        )
        try:
            archive = zipfile.ZipFile(package_copy, "r")
        except (OSError, zipfile.BadZipFile) as error:
            raise AuthenticodeSigningError(
                "signed Velopack full package is not a valid ZIP"
            ) from error
        with archive:
            matches: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                pure = _safe_package_entry(info.filename)
                if pure.name not in {MAIN_EXE, UPDATE_EXE}:
                    continue
                if pure.name in matches:
                    raise AuthenticodeSigningError(
                        f"signed package contains duplicate {pure.name}"
                    )
                if info.is_dir() or info.flag_bits & 0x1:
                    raise AuthenticodeSigningError(
                        f"signed package {pure.name} is not a readable regular entry"
                    )
                if info.file_size < 1 or info.file_size > MAX_VERIFIED_PE_BYTES:
                    raise AuthenticodeSigningError(
                        f"signed package {pure.name} exceeds the bounded verification size"
                    )
                matches[pure.name] = info
            if set(matches) != {MAIN_EXE, UPDATE_EXE}:
                raise AuthenticodeSigningError(
                    "signed package must contain exactly one main app and Update.exe"
                )

            for index, required_name in enumerate((MAIN_EXE, UPDATE_EXE), start=1):
                info = matches[required_name]
                extracted = root / f"package-{index}-{required_name}"
                digest = sha256()
                size = 0
                with archive.open(info, "r") as source, extracted.open("xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != info.file_size:
                    raise AuthenticodeSigningError(
                        f"signed package {required_name} changed during extraction"
                    )
                signature = _verify_authenticode_file(extracted)
                records.append(
                    {
                        "path": f"{package_name}!/{info.filename}",
                        "sha256": "sha256:" + digest.hexdigest(),
                        "size": size,
                        **signature,
                    }
                )

    signer_thumbprints = {str(item["signer_thumbprint"]) for item in records}
    if len(signer_thumbprints) != 1:
        raise AuthenticodeSigningError(
            "Setup, main executable and Update.exe do not share one signer identity"
        )
    return {
        "backend": policy.backend,
        "policy_sha256": policy_sha256,
        "profile": {
            "endpoint": policy.endpoint,
            "code_signing_account_name": policy.code_signing_account_name,
            "certificate_profile_name": policy.certificate_profile_name,
        },
        "timestamp_required": policy.timestamp_required,
        "signer_thumbprint": next(iter(signer_thumbprints)),
        "verified_files": records,
    }
