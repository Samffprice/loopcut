"""Compose the R4 measured gate and unaltered render/UI evidence with Pillow."""
import argparse
import json
from pathlib import Path


def sheet(paths, labels, destination, size=(640, 640), columns=2):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    width, height = size
    image = Image.new('RGB', (width * columns, (height + 38) * ((len(paths) + columns - 1) // columns)), '#101822')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 19)
    for i, (path, label) in enumerate(zip(paths, labels)):
        x, y = i % columns * width, i // columns * (height + 38)
        with Image.open(path) as original:
            image.paste(ImageOps.contain(original.convert('RGB'), (width, height)), (x, y + 38))
        draw.text((x + 12, y + 9), label, font=font, fill='white')
    image.save(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--gui', type=Path, required=True)
    args = parser.parse_args()
    planned = json.loads((args.run / 'planner-results.json').read_bytes())
    if len(planned) != 22:
        raise ValueError('The complete gate needs 22 recorded cases')
    rows = []
    for item in planned:
        directory = args.run / item['fixture']
        result = json.loads((directory / 'result.json').read_bytes())
        report = json.loads((directory / 'local-validation.json').read_bytes())
        if not result['production_pass'] or result.get('unexpected_error'):
            raise ValueError('Incomplete client flow: ' + item['fixture'])
        standard = result['outcome'] == 'applied'
        invariants = report['canonicalization']['invariants'] if standard else {}
        rows.append({'fixture': item['fixture'], 'tier': 'standard' if standard else 'experimental',
            'bones': item['joints'], 'inside_fraction': report['inside']['fraction'],
            'worker_inside_fraction': result.get('worker_inside_fraction'),
            'rest_displacement_metres': report['rest_delta_metres'],
            'joint_displacement_metres': invariants.get('max_joint_displacement_metres', result.get('head_error')),
            'weights_identical': invariants.get('weights_identical', result.get('weight_error') == 0),
            'native_axes_error': result.get('axes_error'),
            'hinges_passed': sum(h['passed'] for h in report['hinges']),
            'original_preserved': result['original_preserved'], 'fake_credits': result['fake_credits'],
            'client_pass': True})
    counts = {'standard': sum(r['tier'] == 'standard' for r in rows),
              'experimental': sum(r['tier'] == 'experimental' for r in rows),
              'hinges_passed': sum(r['hinges_passed'] for r in rows),
              'max_rest_displacement_metres': max(r['rest_displacement_metres'] for r in rows),
              'real_spend_usd': 0, 'release_accepted': False}
    (args.run / 'fixture-results.json').write_text(json.dumps({'counts': counts, 'fixtures': rows}, indent=2))
    lines = ['# R4 client gate', '', '| Fixture | Tier | Bones | Preserve / apply | Fake credits |',
             '|---|---|---:|---|---:|']
    lines += [f"| {r['fixture']} | {r['tier']} | {r['bones']} | Pass | {r['fake_credits']} |" for r in rows]
    lines += ['', 'Standard: 17; experimental: 5. $0 real spend. These are client technical gates, not release approval.', '']
    (args.run / 'fixture-results.md').write_text('\n'.join(lines))
    ids = ['Q1', 'Q2', 'B5', 'R18', 'P2']
    sheet([args.run / i / 'rest.png' for i in ids],
          [i + ' - experimental native rig, preserved original' for i in ids],
          args.run / 'experimental-contact-sheet.png', size=(640, 640), columns=3)
    frames = [1, 7, 13, 19]
    sheet([args.run / 'Q1' / f'legs-{i:02d}.png' for i in frames],
          [f'Q1 - plain bpy leg keys - frame {i}' for i in frames], args.run / 'Q1/leg-contact-sheet.png')
    sheet([args.gui / 'quote.png', args.gui / 'result.png'], ['Actual GUI quote', 'Actual Q1 experimental result'],
          args.gui / 'quote-result-contact-sheet.png', size=(1440, 900), columns=1)
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
