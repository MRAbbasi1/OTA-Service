# 1. Hardware Silicon Anchor via Factory eFuse MAC

Date: 2026-10-17

## Status

Accepted

## Context

Connected IoT devices frequently use external Ethernet controllers (such as W5500) or Wi-Fi PHY modules where the network MAC address is either software-configurable, ephemeral, or dynamically assigned by the physical network interface. In security models that rely on network MAC addresses for device identification:
1. MAC address spoofing allows unauthorized hardware to pose as legitimate enrolled devices.
2. Swapping a peripheral PHY board changes the device's apparent identity, breaking device management tracking.

## Decision

We anchor device hardware identity to the **factory-burned raw eFuse MAC** (`ESP.getEfuseMac()` lower 6 bytes):
- Stored and compared as uppercase colon-separated hex format (`AA:BB:CC:DD:EE:FF`).
- Required in the `X-Device-Mac` HTTP header for all manifest and firmware download requests.
- Validated on the server alongside the provisioned serial number and hashed device token.

## Consequences

- **Positive**: Hardware identity is physically immutable and silicon-bound; swapping an Ethernet daughterboard or changing network interfaces does not alter the device identity.
- **Positive**: Eliminates network MAC spoofing attacks against the OTA delivery pipeline.
- **Negative**: Factory provisioning must read and enroll the physical eFuse MAC during manufacturing rather than scanning an external packaging barcode containing the network MAC.
