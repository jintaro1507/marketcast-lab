#!/usr/bin/env python3
"""
Weekly Marketcast JSON Schema フィクスチャ検証スクリプト

用途: W1-1 の JSON Schema 検証テスト
実行: python3 scripts/validate_weekly_schemas.py

依存: jsonschema >= 4.0 (Draft 2020-12 サポート)
インストール: pip install jsonschema
"""

import json
import sys
from pathlib import Path

# スクリプトの位置からプロジェクトルートを特定
PROJECT_ROOT = Path(__file__).parent.parent
SCHEMAS_DIR = PROJECT_ROOT / 'schemas'
FIXTURES_DIR = PROJECT_ROOT / 'tests' / 'fixtures' / 'weekly'

try:
    import jsonschema
    from jsonschema import validate, ValidationError
    from jsonschema.validators import validator_for
except ImportError:
    print("ERROR: jsonschema が見つかりません。")
    print("インストール: pip install jsonschema")
    print("または venv 経由で実行してください。")
    sys.exit(1)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def load_json(path: Path) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def run_test(name: str, fixture_path: Path, schema: dict, expect_valid: bool) -> bool:
    try:
        fixture = load_json(fixture_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  [{FAIL}] {name}: ファイル読み込みエラー: {e}")
        return False

    # JSON Schema Draft 2020-12 バリデーター
    validator_cls = validator_for(schema)
    validator = validator_cls(schema)

    errors = list(validator.iter_errors(fixture))

    if expect_valid:
        if not errors:
            print(f"  [{PASS}] {name}")
            return True
        else:
            print(f"  [{FAIL}] {name}: 有効なはずが検証エラー")
            for e in errors[:3]:
                print(f"         → {e.json_path}: {e.message}")
            return False
    else:
        if errors:
            print(f"  [{PASS}] {name}: 期待通りエラー検出 ({len(errors)}件)")
            for e in errors[:2]:
                print(f"         → {e.json_path}: {e.message[:80]}")
            return True
        else:
            print(f"  [{FAIL}] {name}: 無効なはずが検証をパス（エラー未検出）")
            return False


def main():
    print("=" * 60)
    print("Weekly Marketcast JSON Schema フィクスチャ検証")
    print(f"jsonschema version: {jsonschema.__version__ if hasattr(jsonschema, '__version__') else 'unknown'}")
    print("=" * 60)

    free_teaser_schema = load_json(SCHEMAS_DIR / 'weekly_free_teaser.schema.json')
    paid_body_schema   = load_json(SCHEMAS_DIR / 'weekly_paid_body.schema.json')

    results = []

    print("\n[free_teaser スキーマ検証]")
    results.append(run_test(
        "valid fixture → pass",
        FIXTURES_DIR / 'free_teaser_valid.json',
        free_teaser_schema,
        expect_valid=True,
    ))
    results.append(run_test(
        "score フィールドあり → fail（additionalProperties: false）",
        FIXTURES_DIR / 'free_teaser_invalid_score.json',
        free_teaser_schema,
        expect_valid=False,
    ))

    print("\n[paid_body スキーマ検証]")
    results.append(run_test(
        "valid fixture → pass",
        FIXTURES_DIR / 'paid_body_valid.json',
        paid_body_schema,
        expect_valid=True,
    ))
    results.append(run_test(
        "gold end_value 非 null → fail（restricted 生値混入）",
        FIXTURES_DIR / 'paid_body_invalid_restricted_value.json',
        paid_body_schema,
        expect_valid=False,
    ))

    # 追加: 配列件数違反テスト（インラインで生成）
    print("\n[追加: インライン検証]")

    # observation_points が 2件（min 3 以下）
    import copy
    valid_pb = load_json(FIXTURES_DIR / 'paid_body_valid.json')
    too_few_obs = copy.deepcopy(valid_pb)
    too_few_obs['observation_points'] = ["観測ポイント1", "観測ポイント2"]
    validator_cls = validator_for(paid_body_schema)
    errors = list(validator_cls(paid_body_schema).iter_errors(too_few_obs))
    if errors:
        print(f"  [{PASS}] observation_points 2件（min=3）→ fail: {errors[0].message[:60]}")
        results.append(True)
    else:
        print(f"  [{FAIL}] observation_points 2件: エラー未検出")
        results.append(False)

    # asset_summaries が 5件（min/max=6）
    too_few_assets = copy.deepcopy(valid_pb)
    too_few_assets['asset_summaries'] = too_few_assets['asset_summaries'][:5]
    errors = list(validator_cls(paid_body_schema).iter_errors(too_few_assets))
    if errors:
        print(f"  [{PASS}] asset_summaries 5件（min=max=6）→ fail: {errors[0].message[:60]}")
        results.append(True)
    else:
        print(f"  [{FAIL}] asset_summaries 5件: エラー未検出")
        results.append(False)

    # free_teaser に additionalProperty
    valid_ft = load_json(FIXTURES_DIR / 'free_teaser_valid.json')
    extra_prop = copy.deepcopy(valid_ft)
    extra_prop['unknown_field'] = "should_not_exist"
    ft_validator_cls = validator_for(free_teaser_schema)
    errors = list(ft_validator_cls(free_teaser_schema).iter_errors(extra_prop))
    if errors:
        print(f"  [{PASS}] free_teaser に unknown_field → fail: {errors[0].message[:60]}")
        results.append(True)
    else:
        print(f"  [{FAIL}] free_teaser に unknown_field: エラー未検出")
        results.append(False)

    # sp500 の end_value が非 null（restricted 生値）
    invalid_sp500 = copy.deepcopy(valid_pb)
    for a in invalid_sp500['asset_summaries']:
        if a['asset_key'] == 'sp500':
            a['end_value'] = 5000.0
    errors = list(validator_cls(paid_body_schema).iter_errors(invalid_sp500))
    if errors:
        print(f"  [{PASS}] sp500 end_value 非 null → fail: {errors[0].message[:60]}")
        results.append(True)
    else:
        print(f"  [{FAIL}] sp500 end_value 非 null: エラー未検出")
        results.append(False)

    # wti の restricted=true（非 restricted 資産への誤設定）
    invalid_wti_restricted = copy.deepcopy(valid_pb)
    for a in invalid_wti_restricted['asset_summaries']:
        if a['asset_key'] == 'wti':
            a['restricted'] = True
    errors = list(validator_cls(paid_body_schema).iter_errors(invalid_wti_restricted))
    if errors:
        print(f"  [{PASS}] wti restricted=true → fail: {errors[0].message[:60]}")
        results.append(True)
    else:
        print(f"  [{FAIL}] wti restricted=true: エラー未検出")
        results.append(False)

    # 集計
    passed = sum(results)
    total  = len(results)
    print(f"\n{'=' * 60}")
    print(f"結果: {passed}/{total} PASS")
    if passed == total:
        print("全テスト PASS")
        sys.exit(0)
    else:
        print(f"FAIL: {total - passed} 件")
        sys.exit(1)


if __name__ == '__main__':
    main()
