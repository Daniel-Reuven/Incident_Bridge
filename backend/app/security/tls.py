"""
The HTTPS certificate: a generated self-signed one for testing, or the
admin's own files (required in production - app/server_settings.py).

Generated (testing only):
    <database folder>/tls/self-signed.crt   the certificate (public)
    <database folder>/tls/self-signed.key   its private key, ENCRYPTED with a
                                            random password kept in the
                                            encrypted database ("tls_generated")
    Names: localhost, this computer's name, 127.0.0.1, ::1 and its own IP
    addresses; valid for a year. Each browser warns about it once.
    Before the setup there is no database (and the data folders must stay
    empty): a throwaway one in a temporary folder serves the setup page, and
    the first start after the setup makes the lasting one.

Files (testing or production): TLS_CERT_FILE / TLS_KEY_FILE (+ TLS_KEY_PASSWORD
when the key is encrypted), or the same on the Server settings page. The
certificate file may hold the chain (the certificate first, then the
intermediates). check_files() loads the pair the way the server will (so a
key that does not match is found now, not at startup), and in production
also verifies the chain against the public certificate authorities (certifi)
and refuses a self-signed or expired certificate.
"""

import datetime as dt
import functools
import ipaddress
import secrets
import socket
import ssl
import warnings
from pathlib import Path
from typing import List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

GENERATED_CERT = "self-signed.crt"
GENERATED_KEY = "self-signed.key"
SETTING = "tls_generated"                   # {"password": ...} in the encrypted settings
VALID_DAYS = 365
EXPIRY_WARNING_DAYS = 30


def generated_dir(db_path: str) -> Path:
    if db_path == ":memory:":
        raise ValueError("An in-memory database has no data folder for a generated certificate.")
    return Path(db_path).resolve().parent / "tls"


def _names() -> List[x509.GeneralName]:
    """localhost, this computer's names and its IP addresses (best effort)."""
    dns = {"localhost"}
    ips = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
    host = socket.gethostname()
    for name in (host, socket.getfqdn()):
        if name and not name.replace(".", "").isdigit():
            dns.add(name.lower())
    try:
        for info in socket.getaddrinfo(host, None):
            ips.add(ipaddress.ip_address(info[4][0].split("%")[0]))
    except (OSError, ValueError):
        pass
    try:                                     # the address used to reach other computers (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            ips.add(ipaddress.ip_address(probe.getsockname()[0]))
    except OSError:
        pass
    return [x509.DNSName(n) for n in sorted(dns)] + [x509.IPAddress(i) for i in sorted(ips, key=str)]


def generate_self_signed(folder: Path, password: str) -> dict:
    """Write a new self-signed certificate and its encrypted key to `folder`; returns its info."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Incident Bridge (self-signed, testing only)")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_names()), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / GENERATED_CERT).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = folder / GENERATED_KEY
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(password.encode())))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return certificate_info(folder / GENERATED_CERT)


def ensure_generated(db_path: str, settings, folder: Optional[Path] = None) -> tuple:
    """(cert path, key path, password) of the generated certificate - made now if missing or expired."""
    folder = folder or generated_dir(db_path)
    stored = settings.get(SETTING) or {}
    cert, key = folder / GENERATED_CERT, folder / GENERATED_KEY
    fresh = cert.is_file() and key.is_file() and stored.get("password")
    if fresh:
        info = certificate_info(cert)
        fresh = info["days_left"] > 0
    if not fresh:
        global _saved_before_log
        stored = {"password": secrets.token_urlsafe(32)}
        generate_self_signed(folder, stored["password"])
        settings.set(SETTING, stored)
        _saved_before_log = True
    return cert, key, stored["password"]


_saved_before_log = False


def saved_before_log() -> bool:
    """
    True (once) when serve_app.py saved the key's password before the app -
    and its activity log - started: app/api/lifecycle.py then fingerprints
    it, so the integrity check does not report it as added by hand.
    """
    global _saved_before_log
    saved, _saved_before_log = _saved_before_log, False
    return saved


def regenerate(db_path: str, settings) -> dict:
    password = secrets.token_urlsafe(32)
    info = generate_self_signed(generated_dir(db_path), password)
    settings.set(SETTING, {"password": password})
    return info


def _load_chain(path: Path) -> List[x509.Certificate]:
    certs = x509.load_pem_x509_certificates(path.read_bytes())
    if not certs:
        raise ValueError("no certificate in it")
    return certs


def certificate_info(path: Path) -> dict:
    cert = _load_chain(Path(path))[0]
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        alt = [str(v) for v in names.get_values_for_type(x509.DNSName)] + \
              [str(v) for v in names.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        alt = []
    common = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    organization = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    expires = cert.not_valid_after_utc
    return {
        "subject": common[0].value if common else cert.subject.rfc4514_string(),
        "issuer": (issuer[0].value if issuer else cert.issuer.rfc4514_string())
                  + (f" ({organization[0].value})" if organization else ""),
        "names": alt,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": expires.isoformat(),
        "days_left": (expires - dt.datetime.now(dt.timezone.utc)).days,
        "self_signed": cert.issuer == cert.subject,
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
    }


@functools.lru_cache(maxsize=1)
def _public_store():
    """The public certificate authorities browsers trust (certifi's bundle, Mozilla's list)."""
    import certifi
    from cryptography.x509.verification import Store
    with warnings.catch_warnings():             # one old root in the bundle has a non-positive serial number
        warnings.simplefilter("ignore")
        return Store(x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes()))


def check_files(cert_file: str, key_file: str, password: str = "", mode: str = "testing",
                trust=None, now: Optional[dt.datetime] = None) -> dict:
    """
    {"ok", "problems", "warnings", "info"} for a certificate + key pair. `trust`
    (a cryptography Store) replaces the public authorities - for tests.
    """
    problems: List[str] = []
    warnings: List[str] = []
    info = None
    cert_path, key_path = Path(cert_file or ""), Path(key_file or "")
    for label, path in (("certificate", cert_path), ("key", key_path)):
        if not str(path) or str(path) == "." or not path.is_file():
            problems.append(f"The {label} file {path} was not found.")
    if problems:
        return {"ok": False, "problems": problems, "warnings": warnings, "info": None}
    try:
        chain = _load_chain(cert_path)
        info = certificate_info(cert_path)
    except ValueError as exc:
        return {"ok": False, "problems": [f"{cert_path} is not a PEM certificate file ({exc})."], "warnings": [],
                "info": None}
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path), password=password or None)
    except ssl.SSLError:
        problems.append("The key does not fit this certificate, or the key's password is wrong.")
    except (OSError, TypeError) as exc:
        problems.append(f"The key could not be read: {exc}.")
    now = now or dt.datetime.now(dt.timezone.utc)
    leaf = chain[0]
    if leaf.not_valid_after_utc <= now:
        problems.append(f"The certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d}.")
    elif leaf.not_valid_before_utc > now:
        problems.append(f"The certificate is not valid until {leaf.not_valid_before_utc:%Y-%m-%d}.")
    elif info["days_left"] < EXPIRY_WARNING_DAYS:
        warnings.append(f"The certificate expires in {info['days_left']} day(s) - renew it.")
    if mode == "production":
        if info["self_signed"]:
            problems.append("Production needs a certificate from a certificate authority - this one is self-signed.")
        else:
            problems += _verify_chain(chain, trust, now)
    elif info["self_signed"]:
        warnings.append("Self-signed: fine for testing (each browser warns once); production needs a certificate "
                        "from a certificate authority.")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "info": info}


def _verify_chain(chain: List[x509.Certificate], trust, now: dt.datetime) -> List[str]:
    from cryptography.x509.verification import PolicyBuilder, VerificationError
    leaf = chain[0]
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = names.get_values_for_type(x509.DNSName)
        ips = names.get_values_for_type(x509.IPAddress)
    except x509.ExtensionNotFound:
        dns, ips = [], []
    if dns:
        subject = x509.DNSName("host." + dns[0][2:] if dns[0].startswith("*.") else dns[0])   # a wildcard: any name under it
    elif ips:
        subject = x509.IPAddress(ips[0])
    else:
        return ["The certificate names no host (no subject alternative names)."]
    try:
        verifier = PolicyBuilder().store(trust or _public_store()).time(now).build_server_verifier(subject)
        verifier.verify(leaf, chain[1:])
    except VerificationError as exc:
        return [f"The certificate does not chain to a trusted certificate authority ({exc}). If it does, put the "
                f"intermediate certificate(s) after it in the same file."]
    return []
