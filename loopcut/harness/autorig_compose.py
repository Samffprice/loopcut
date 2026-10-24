"""Compose measured R3 evidence with the existing hash-locked M1 Pillow environment.

No rendering or invented successful panels. Run after the Blender harness exits.
"""
import argparse
import json
from pathlib import Path

NEW = {'B4', 'B7', 'R12', 'R13', 'R16'}


def main():
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--gui', type=Path)
    args = parser.parse_args()
    font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
    font = ImageFont.truetype(font_path, 19)
    small = ImageFont.truetype(font_path, 16)
    large = ImageFont.truetype(font_path, 25)
    planner = json.loads((args.run / 'planner-results.json').read_text())
    if len(planner) != 22:
        raise ValueError('Expected all 22 planner cases')
    rows, tiles = [], []
    markdown = ['# Client R3 fixture results', '',
        'All cases use ordinary local admission, quote and typed-result handling. No anatomy bypass is allowed.',
        'Uncertain shapes receive a warning on the quote. Unsupported skeletons are refused without a charge.', '',
        '| Fixture | Planner | Local check | Blender result | Walk samples | Full client pass |',
        '|---|---|---|---|---|---|']
    for planned in planner:
        fid = planned['fixture']
        folder = args.run / fid
        result = json.loads((folder / 'result.json').read_text())
        local = result.get('preflight', {}).get('eligible') is True
        outcome = result.get('outcome', 'incomplete')
        applied = outcome == 'applied' and not result.get('unexpected_error')
        row = {'fixture': fid, 'planner': planned['outcome'], 'joint_count': planned['joints'],
               'local_eligible': local, 'blender_outcome': outcome,
               'diagnostic_preflight_bypass': result['test_only_preflight_bypass'],
               'production_pass': result['production_pass'], 'original_preserved': result.get('original_preserved', False)}
        if applied:
            report = json.loads((folder / 'local-validation.json').read_text())
            row.update(inside_fraction=report['inside']['fraction'],
                hinge_passes=sum(h['passed'] for h in report['hinges']),
                rest_displacement_metres=report['canonicalization']['invariants']['max_rest_mesh_displacement_metres'],
                joint_displacement_metres=report['canonicalization']['invariants']['max_joint_displacement_metres'],
                weights_identical=report['canonicalization']['invariants']['weights_identical'],
                bone_count=len(report['canonicalization']['labels']), hands=report['canonicalization']['hands'])
            sheet = Image.new('RGB', (1440, 1100), '#101822')
            draw = ImageDraw.Draw(sheet)
            draw.text((18, 12), fid + ' | rest and shipped walk on preserved originals', font=large, fill='white')
            draw.text((18, 50), f"{row['bone_count']} bones; inside {row['inside_fraction']:.1%}; "
                      f"hinges {row['hinge_passes']}/4; " + ('local check passed' if local else 'DIAGNOSTIC: local check refused'),
                      font=font, fill='#9cd9dd')
            samples = [('rest.png', 'Rest, three-quarter'), ('rest-side.png', 'Rest, side'),
                       ('walk-1.png', 'Walk frame 1'), ('walk.png', 'Walk frame 13'),
                       ('walk-25.png', 'Walk frame 25'), ('walk-37.png', 'Walk frame 37')]
            for i, (name, title) in enumerate(samples):
                x, y = i % 3 * 480, 90 + i // 3 * 500
                with Image.open(folder / name) as original:
                    sheet.paste(ImageOps.contain(original.convert('RGB'), (480, 470)), (x, y))
                draw.text((x + 14, y + 470), title, font=font, fill='white')
            sheet.save(folder / 'contact-sheet.jpg', quality=94)
            if fid in NEW:
                tile = Image.new('RGB', (640, 390), '#101822')
                d = ImageDraw.Draw(tile)
                for i, name in enumerate(('rest.png', 'walk.png')):
                    with Image.open(folder / name) as original:
                        tile.paste(ImageOps.contain(original.convert('RGB'), (320, 320)), (i * 320, 0))
                d.text((12, 324), f'{fid} | rest / walk frame 13 | {row["bone_count"]} bones', font=font, fill='white')
                d.text((12, 355), 'Local eligible' if local else 'Diagnostic: preflight refused', font=small, fill='#9cd9dd')
                tiles.append(tile)
        plan_text = str(planned['joints']) + ' labels' if planned['outcome'] == 'pass' else planned['outcome']
        markdown.append(f"| {fid} | {plan_text} | {'Pass' if local else 'Refused'} | "
            f"{'Applied' if applied else outcome}{' (diagnostic)' if applied and not local else ''} | "
            f"{'1/13/25/37 pass' if applied else 'Not run'} | {'Yes' if row['production_pass'] else 'No'} |")
        rows.append(row)
    counts = {'fixtures': len(rows), 'intended_humanoids': 19,
              'planner_pass': sum(r['planner'] == 'pass' for r in rows),
              'blender_apply_pass': sum(r['blender_outcome'] == 'applied' and r['original_preserved'] for r in rows),
              'production_pass': sum(r['production_pass'] for r in rows),
              'typed_refusals': sum(r['blender_outcome'] == 'unsupported_anatomy' for r in rows),
              'real_spend_usd': 0, 'release_accepted': False}
    markdown += ['', 'Totals: ' + json.dumps(counts), '',
        'Sampled workbench frames are technical review evidence, not continuous playback, fine-hand/contact,',
        'rigid-accessory, facial, cloth or release approval. See visual-review.json for the actual inspection.', '']
    (args.run / 'fixture-results.json').write_text(json.dumps({'counts': counts, 'fixtures': rows}, indent=2))
    (args.run / 'fixture-results.md').write_text('\n'.join(markdown))
    overview = Image.new('RGB', (1920, 80 + 390 * ((len(tiles) + 2) // 3)), '#080f17')
    draw = ImageDraw.Draw(overview)
    draw.text((18, 12), 'Newly admitted full client flows | R3 | retained joints and original meshes', font=large, fill='white')
    draw.text((18, 48), 'Each pair is rest / walk frame 13. Open per-fixture sheets for side and other walk samples.', font=font, fill='#b9c5d1')
    for i, tile in enumerate(tiles):
        overview.paste(tile, (i % 3 * 640, 80 + i // 3 * 390))
    overview.save(args.run / 'newly-passing-contact-sheet.jpg', quality=94)
    if args.gui:
        groups = {
            'success-stages': ['checking', 'uploading', 'quote', 'waiting', 'rigging', 'applying', 'validating', 'result', 'free-test-pose'],
            'refund-stages': ['refund-' + n for n in ['checking', 'uploading', 'quote', 'waiting', 'rigging', 'applying', 'validating', 'pending', 'result']],
            'anatomy-stages': ['anatomy-' + n for n in ['checking', 'uploading', 'quote', 'waiting', 'rigging', 'result']],
            'results-review': ['result', 'free-test-pose', 'refund-result', 'anatomy-result'],
        }
        for name, files in groups.items():
            width, cols = (1000, 2) if name == 'results-review' else (640, 3)
            cell = round(width * 1626 / 2880) + 34
            sheet = Image.new('RGB', (width * cols, cell * ((len(files) + cols - 1) // cols)), '#101822')
            draw = ImageDraw.Draw(sheet)
            for i, filename in enumerate(files):
                x, y = i % cols * width, i // cols * cell
                with Image.open(args.gui / (filename + '.png')) as original:
                    sheet.paste(ImageOps.contain(original.convert('RGB'), (width, cell - 34)), (x, y))
                draw.text((x + 10, y + cell - 28), filename, font=font, fill='white')
            sheet.save(args.gui / (name + '.jpg'), quality=94)
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
