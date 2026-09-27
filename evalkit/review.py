from pathlib import Path
from .common import canonical


def build_page(data, truth, output):
    template = (Path(__file__).parent / 'review.html').read_text(encoding='utf-8')
    # Prevent a merchant-controlled </script> sequence from leaving the JSON block.
    payload = canonical({'dataset': data, 'truth': truth}).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(template.replace('__EMBEDDED_DATA__', payload))
