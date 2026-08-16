from __future__ import annotations

from lxml import etree
from signxml import XMLSigner, methods
from signxml.algorithms import DigestAlgorithm, SignatureMethod

from app.certificate import CertificateMaterial

C14N_ALGORITHM = 'http://www.w3.org/TR/2001/REC-xml-c14n-20010315'
DS_NS = 'http://www.w3.org/2000/09/xmldsig#'


def sign_signed_service_metadata(
    root: etree._Element,
    material: CertificateMaterial,
) -> etree._Element:
    """Enveloped XML-DSig nad SignedServiceMetadata (format kao PU demo MPS)."""
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
        c14n_algorithm=C14N_ALGORITHM,
    )
    signed = signer.sign(
        root,
        key=material.private_key_pem,
        cert=material.certificate_pem,
        exclude_c14n_transform_element=True,
    )
    x509_data = signed.find(f'.//{{{DS_NS}}}X509Data')
    if x509_data is not None:
        subject = etree.SubElement(x509_data, f'{{{DS_NS}}}X509SubjectName')
        subject.text = material.subject
        cert_el = x509_data.find(f'{{{DS_NS}}}X509Certificate')
        if cert_el is not None and cert_el.text:
            cert_el.text = cert_el.text.replace('\n', '')
    return signed
