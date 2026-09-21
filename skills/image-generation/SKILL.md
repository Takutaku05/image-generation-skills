---
name: image-generation
description: Gemini API (Nano Banana) で画像を生成・編集する。画像、イラスト、アイコン、サムネイルなどの作成、既存画像のリスタイル、または Google AI Studio の画像モデルを指定された時に使う。実データのグラフや画面キャプチャの作成には使わない。
---

# Image generation with Gemini

このスキルは Claude Code と Codex で共通の `SKILL.md` と Python スクリプトを使う。実行環境に画像生成専用ツールの利用を求める上位の指示がある場合は、それに従う。

## Setup

- Python 3.10 以上と、同梱の `requirements.txt` に記載したパッケージが必要。仮想環境へ導入した場合はその Python でスクリプトを実行する。導入例はリポジトリの `README.md` を参照。
- API キーは `GEMINI_API_KEY` 環境変数で渡す。ローカルの `.env` を使う場合は、このスキルの `.env.example` を参考にする。探索順は `GEMINI_ENV_FILE` で指定したファイル → 実行ディレクトリの `.env` → スキル内の `.env`。既存の環境変数が優先される。
- キーの値を表示・記録・コミットしない。参照画像やプロンプトを外部 API に送る前に、ユーザーの意図と共有範囲を確認する。
- スクリプトの場所は、この `SKILL.md` があるディレクトリを基準に `scripts/generate_image.py` として解決する。Claude Code 専用の変数や固定の絶対パスを使わない。

## Workflow

1. 依頼から主題、用途、縦横比、参考画像の有無を決める。不明点が完成物に影響する時だけ確認する。
2. **生成 API を呼ぶ前に**、枚数・tier・解像度と概算を示し、この依頼に対する課金の承認を確認する。過去の利用やこのスキルの導入を承認とみなさない。概算は画像出力分のみで、入力分などや料金改定は含まない。必要に応じて [公式料金表](https://ai.google.dev/gemini-api/docs/pricing) を確認する。承認が得られなければ生成しない。
3. `--dry-run` でモデル、保存先、画像出力分の概算を確認する。これは画像を生成しないが、モデル解決のため API に問い合わせることがある。`--list-models` も画像を生成しない。
4. 承認された範囲で生成する。試作が有用なら `--tier proto`、完成用なら `--tier final` を選ぶ。複雑な文字入れなどで `--tier ultra` が必要なら、そのモデルと費用について別途明示的な許可を得る。各生成コマンドに `--confirm-spend`、ultra にはさらに `--confirm-ultra` を付ける。これらのフラグは許可の代わりではなく、許可済みであることの宣言。
5. 生成画像を実際に確認し、ユーザーに環境で利用可能な方法で提示する。出力パス、モデル、概算費用を伝える。追加生成が承認済みの範囲を超える場合は、再度確認する。

| tier | モデル系統 | 用途 |
|---|---|---|
| `proto` | `flash-lite-image` | 低コストの試作 |
| `final` | `flash-image` | 通常の完成画像 |
| `ultra` | `pro-image` | 複雑な構図や文字入れ。個別承認が必要 |

モデルは利用可能な同系統から自動選択される。`--model` で固定する場合も tier と同じ系統のみ指定できる。モデルの提供状況や料金は変わるため、固定のモデル ID や古い料金を前提にしない。

## Usage

以下はリポジトリ直下での例。別の場所にインストールした場合は、その場所にある `scripts/generate_image.py` を指定する。

```bash
python3 skills/image-generation/scripts/generate_image.py --list-models
python3 skills/image-generation/scripts/generate_image.py "A flat vector illustration of a garden, no text" --tier final --aspect 16:9 --dry-run
python3 skills/image-generation/scripts/generate_image.py "A flat vector illustration of a garden, no text" --tier final --aspect 16:9 --confirm-spend
python3 skills/image-generation/scripts/generate_image.py "Recolor this image in blue" --ref input.png --tier final -o output.png --confirm-spend
python3 skills/image-generation/scripts/generate_image.py "A detailed poster" --tier ultra --confirm-spend --confirm-ultra
```

`--out-dir` 省略時は実行ディレクトリの `generated-images/YYYYMMDD/` に保存する。返却された画像の MIME に合わせて拡張子が決まるので、実際の保存先は標準出力の JSON `files[].path` で確認する。既存画像は上書きしない。画像と同名の `.json` にはプロンプトや参照パスが入るため、公開前に確認する。不要なら `--no-meta` を使う。このリポジトリ内の既定保存先は Git から除外されるが、他のプロジェクトで実行する場合はそのプロジェクトの除外設定を確認する。

主な引数: `--tier proto|final|ultra`、`--aspect`、`--size`、`-n`（枚数）、`--ref`（参考画像）、`--out-dir`、`-o`、`--model`、`--dry-run`。終了コードは 0 が成功、1 が設定・引数エラー、2 が課金または ultra の未承認、3 が画像なし、4 が一部失敗。
