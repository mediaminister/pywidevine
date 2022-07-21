import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from zlib import crc32

import click
import requests
from construct import ConstructError
from unidecode import unidecode, UnidecodeError

from pywidevine import __version__
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.license_protocol_pb2 import LicenseType, FileHashes


@click.group(invoke_without_command=True)
@click.option("-v", "--version", is_flag=True, default=False, help="Print version information.")
@click.option("-d", "--debug", is_flag=True, default=False, help="Enable DEBUG level logs.")
def main(version: bool, debug: bool) -> None:
    """pywidevine—Python Widevine CDM implementation."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)
    log = logging.getLogger()

    copyright_years = 2022
    current_year = datetime.now().year
    if copyright_years != current_year:
        copyright_years = f"{copyright_years}-{current_year}"

    log.info(f"pywidevine version {__version__} Copyright (c) {copyright_years} rlaphoenix")
    log.info("https://github.com/rlaphoenix/pywidevine")
    if version:
        return


@main.command(name="license")
@click.argument("device", type=Path)
@click.argument("pssh", type=str)
@click.argument("server", type=str)
@click.option("-t", "--type", "type_", type=click.Choice(LicenseType.keys(), case_sensitive=False),
              default="STREAMING",
              help="License Type to Request.")
@click.option("-r", "--raw", is_flag=True, default=False,
              help="PSSH is Raw.")
@click.option("-p", "--privacy", is_flag=True, default=False,
              help="Use Privacy Mode, off by default.")
def license_(device: Path, pssh: str, server: str, type_: str, raw: bool, privacy: bool):
    """
    Make a License Request for PSSH to SERVER using DEVICE.
    It will return a list of all keys within the returned license.

    This expects the Licence Server to be a simple opaque interface where the Challenge
    is sent as is (as bytes), and the License response is returned as is (as bytes).
    This is a common behavior for some License Servers and is our only option for a generic
    licensing function.

    You may modify this function to change how it sends the Challenge and how it parses
    the License response. However, for non-generic license calls, I recommend creating a
    new script that imports and uses the pywidevine module instead. This generic function
    is only useful as a quick generic license call.

    This is also a great way of showing you how to use pywidevine in your own projects.
    """
    log = logging.getLogger("license")

    # load device
    device = Device.load(device)
    log.info(f"[+] Loaded Device ({device.system_id} L{device.security_level})")
    log.debug(device)

    # load cdm
    cdm = Cdm(device, pssh, raw)
    log.info(f"[+] Loaded CDM with PSSH: {pssh}")
    log.debug(cdm)

    if privacy:
        # get service cert for license server via cert challenge
        service_cert = requests.post(
            url=server,
            data=cdm.service_certificate_challenge
        )
        if service_cert.status_code != 200:
            log.error(f"[-] Failed to get Service Privacy Certificate: [{service_cert.status_code}] {service_cert.text}")
            return
        service_cert = service_cert.content
        cdm.set_service_certificate(service_cert)
        log.info("[+] Set Service Privacy Certificate")
        log.debug(service_cert)

    # get license challenge
    license_type = LicenseType.Value(type_)
    challenge = cdm.get_license_challenge(license_type, privacy_mode=True)
    log.info("[+] Created License Request Message (Challenge)")
    log.debug(challenge)

    # send license challenge
    licence = requests.post(
        url=server,
        data=challenge
    )
    if licence.status_code != 200:
        log.error(f"[-] Failed to send challenge: [{licence.status_code}] {licence.text}")
        return
    licence = licence.content
    log.info("[+] Got License Message")
    log.debug(licence)

    # parse license challenge
    keys = cdm.parse_license(licence)
    log.info("[+] License Parsed Successfully")

    # print keys
    for key in keys:
        log.info(f"[{key.type}] {key.kid.hex}:{key.key.hex()}")


@main.command()
@click.argument("device", type=Path)
@click.pass_context
def test(ctx: click.Context, device: Path):
    """
    Test the CDM code by getting Content Keys for Bitmovin's Art of Motion example.
    https://bitmovin.com/demos/drm
    https://bitmovin-a.akamaihd.net/content/art-of-motion_drm/mpds/11331.mpd

    The device argument is a Path to a Widevine Device (.wvd) file which contains
    the device private key among other required information.
    """
    # The PSSH is the same for all tracks both video and audio.
    # However, this might not be the case for all services/manifests.
    pssh = "AAAAW3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADsIARIQ62dqu8s0Xpa" \
           "7z2FmMPGj2hoNd2lkZXZpbmVfdGVzdCIQZmtqM2xqYVNkZmFsa3IzaioCSEQyAA=="

    # This License Server requires no authorization at all, no cookies, no credentials
    # nothing. This is often not the case for real services.
    license_server = "https://cwip-shaka-proxy.appspot.com/no_auth"

    # Specify OFFLINE if it's a PSSH for a download/offline mode title, e.g., the
    # Download feature on Netflix Apps. Otherwise, use STREAMING or AUTOMATIC.
    license_type = LicenseType.STREAMING

    # If the PSSH is not a valid mp4 pssh box, nor a valid CENC Header (init data) then
    # set this to True, otherwise leave it False.
    raw = False

    # this runs the `cdm license` CLI-command code with the data we set above
    # it will print information as it goes to the terminal
    ctx.invoke(
        license_,
        device=device,
        pssh=pssh,
        server=license_server,
        type_=LicenseType.Name(license_type),
        raw=raw
    )


@main.command()
@click.option("-t", "--type", "type_", type=click.Choice([x.name for x in Device.Types], case_sensitive=False),
              required=True, help="Device Type")
@click.option("-l", "--level", type=click.IntRange(1, 3), required=True, help="Device Security Level")
@click.option("-k", "--key", type=Path, required=True, help="Device RSA Private Key in PEM or DER format")
@click.option("-c", "--client_id", type=Path, required=True, help="Widevine ClientIdentification Blob file")
@click.option("-v", "--vmp", type=Path, required=True, help="Widevine FileHashes Blob file")
@click.option("-o", "--output", type=Path, default=None, help="Output Directory")
@click.pass_context
def create_device(
    ctx: click.Context,
    type_: str,
    level: int,
    key: Path,
    client_id: Path,
    vmp: Optional[Path] = None,
    output: Optional[Path] = None
) -> None:
    """
    Create a Widevine Device (.wvd) file from an RSA Private Key (PEM or DER) and Client ID Blob.
    Optionally also a VMP (Verified Media Path) Blob, which will be stored in the Client ID.
    """
    if not key.is_file():
        raise click.UsageError("key: Not a path to a file, or it doesn't exist.", ctx)
    if not client_id.is_file():
        raise click.UsageError("client_id: Not a path to a file, or it doesn't exist.", ctx)
    if vmp and not vmp.is_file():
        raise click.UsageError("vmp: Not a path to a file, or it doesn't exist.", ctx)

    log = logging.getLogger("create-device")

    device = Device(
        type_=Device.Types[type_.upper()],
        security_level=level,
        flags=None,
        private_key=key.read_bytes(),
        client_id=client_id.read_bytes()
    )

    if vmp:
        new_vmp_data = vmp.read_bytes()
        if device.client_id.vmp_data and device.client_id.vmp_data != new_vmp_data:
            log.warning("Client ID already has Verified Media Path data")
        device.client_id.vmp_data = new_vmp_data

    client_info = {}
    for entry in device.client_id.client_info:
        client_info[entry.name] = entry.value

    wvd_bin = device.dumps()

    name = f"{client_info['company_name']} {client_info['model_name']}"
    if client_info.get("widevine_cdm_version"):
        name += f" {client_info['widevine_cdm_version']}"
    name += f" {crc32(wvd_bin).to_bytes(4, 'big').hex()}"

    try:
        name = unidecode(name.strip().lower().replace(" ", "_"))
    except UnidecodeError as e:
        raise click.ClickException(f"Failed to sanitize name, {e}")

    out_path = (output or Path.cwd()) / f"{name}_{device.system_id}_l{device.security_level}.wvd"
    out_path.write_bytes(wvd_bin)

    log.info(f"Created Widevine Device (.wvd) file, {out_path.name}")
    log.info(f" + Type: {device.type.name}")
    log.info(f" + System ID: {device.system_id}")
    log.info(f" + Security Level: {device.security_level}")
    log.info(f" + Flags: {device.flags}")
    log.info(f" + Private Key: {bool(device.private_key)} ({device.private_key.size_in_bits()} bit)")
    log.info(f" + Client ID: {bool(device.client_id)} ({len(device.client_id.SerializeToString())} bytes)")
    if device.client_id.vmp_data:
        file_hashes_ = FileHashes()
        file_hashes_.ParseFromString(device.client_id.vmp_data)
        log.info(f" + VMP: True ({len(file_hashes_.signatures)} signatures)")
    else:
        log.info(f" + VMP: False")
    log.info(f" + Saved to: {out_path.absolute()}")


@main.command()
@click.argument("device", type=Path)
@click.pass_context
def migrate(ctx: click.Context, device: Path) -> None:
    """Upgrade from earlier versions of the Widevine Device (.wvd) format."""
    if not device.is_file():
        raise click.UsageError("device: Not a path to a file, or it doesn't exist.", ctx)

    log = logging.getLogger("migrate")

    data = bytearray(device.read_bytes())
    if not data.startswith(b"WVD"):
        raise click.UsageError("device: Data does not seem to be a WVD file (magic).", ctx)

    version = data[3]
    if version == 0:
        # we have never used version 0, likely data that just so happened to use the WVD magic
        raise click.UsageError("device: Data does not seem to be a WVD file (v0).", ctx)
    if version == 2:
        raise click.UsageError("device: Data is already migrated to the latest version.", ctx)

    success_message = ""

    # v1 to v2
    if version == 1:
        data[3] = 2  # set version to 2 to allow loading
        data[6] = 0  # blank flags as there's no valid flags that aren't deprecated
        # we can now load it, and loading will ignore the now-removed vmp data and length fields
        success_message = "Successfully migrated from Version 1 to Version 2."

    try:
        new_device = Device.loads(bytes(data))
    except ConstructError as e:
        raise click.UsageError(f"device: Data seems to be corrupt or invalid, {e}", ctx)

    # save
    log.debug(new_device)
    new_device.dump(device)

    log.info(success_message)