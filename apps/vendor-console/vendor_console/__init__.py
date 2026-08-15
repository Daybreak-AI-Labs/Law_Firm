"""Daybreak vendor console — the internal control plane for running Lightwork
as a business (customers, licenses, updates, support, fleet health).

This is **not** the product dashboard a customer runs (`maverick dashboard`).
It is Daybreak-internal mission-control and must never ship to a client. It
holds the license-signing root key, so treat it as a high-value target: run it
behind auth on a trusted network, keep the signing key in a KMS/HSM in prod.
"""

__version__ = "0.1.0"
