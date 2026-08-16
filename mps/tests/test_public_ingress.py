from pathlib import Path


def test_public_traefik_does_not_expose_v1():
    text = Path('/opt/stacks/racunai.hr/docker-compose.yml').read_text()
    mps_block = text.split('  mps:', 1)[1].split('\n  cloudflared:', 1)[0]
    assert 'PathPrefix(`/v1`)' not in mps_block
    assert 'PathPrefix(/v1)' not in mps_block
    assert 'Path(`/v1`)' not in mps_block
    assert 'PathPrefix(`/EracunMPS`)' in mps_block
    assert 'postgis' in mps_block
