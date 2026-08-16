from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DOCUMENT_ID = (
    'busdox-docid-qns::urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice'
    '##urn:cen.eu:en16931:2017#compliant#urn:mfin.gov.hr:cius-2025:1.0'
    '#conformant#urn:mfin.gov.hr:ext-2025:1.0::2.1'
)
PROCESS_ID = 'urn:fdc:eracun.hr:poacc:en16931:any'
PARTICIPANT_SCHEME = 'iso6523-actorid-upis'
DOCUMENT_SCHEME = 'busdox-docid-qns'
PROCESS_SCHEME = 'cenbii-procid-ubl'
TRANSPORT_PROFILE = 'eracun-transport-as4-v1_0'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='MPS_', extra='ignore')

    ap_oib: str = '36619131370'
    ap_party_cn: str = 'FISKAL 2'
    as4_endpoint: str = 'https://as4-test.racunai.hr/EracunAS4/services/msh'
    cert_p12_path: str = '/run/secrets/fiscal-cert/36619131370.F2.2.p12'
    cert_p12_password: str = ''
    ams_proxy_url: str = 'https://cis.porezna-uprava.hr:8515/proxy'
    publisher_id: str = 'MPS36619131370'
    ams_verify_ssl: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
