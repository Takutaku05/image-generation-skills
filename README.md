# Gemini image-generation skill

Gemini API を使って画像を生成・編集する Agent Skill です。同じ [`SKILL.md`](skills/image-generation/SKILL.md) と Python スクリプトを Claude Code / Codex の両方で使えます。生成 API は課金される場合があり、実行にはその依頼についての利用者の承認が必要です。

## 導入

Python 3.10 以上が必要です。以下はこのリポジトリをクローンして、スキル本体を両方のツールから参照する例です。

```bash
git clone https://github.com/Takutaku05/image-generation-skills.git
cd image-generation-skills
python3 -m venv .venv
.venv/bin/python -m pip install -r skills/image-generation/requirements.txt
mkdir -p "$HOME/.claude/skills" "$HOME/.agents/skills"
ln -s "$PWD/skills/image-generation" "$HOME/.claude/skills/image-generation"
ln -s "$PWD/skills/image-generation" "$HOME/.agents/skills/image-generation"
```

`.claude/skills/` は [Claude Code](https://code.claude.com/docs/en/skills)、`.agents/skills/` は [Codex](https://developers.openai.com/docs/build-skills) の読み込み先です。Claude Code では `/image-generation`、Codex では `$image-generation` で明示的に呼び出せます。既に同名のスキルがある場合、`ln -s` は失敗するため、既存の内容を確認してから導入してください。片方だけ使う場合は、そのツール用のリンクだけ作れば構いません。スキルが一覧に出ない場合はセッションを再起動してください。

API キーは `GEMINI_API_KEY` 環境変数に設定するか、[`skills/image-generation/.env.example`](skills/image-generation/.env.example) を同じディレクトリの `.env` にコピーして設定します。実値入り `.env`、生成画像、プロンプトを含む生成後の `.json` は公開しないでください。このリポジトリ内の既定の保存先は `.gitignore` で除外されていますが、別のプロジェクトや保存先で実行した場合はコミット前に確認してください。

## 使用例

リポジトリ直下から実行する例です。仮想環境を作った場合は、その Python を指定します。

```bash
.venv/bin/python skills/image-generation/scripts/generate_image.py "A watercolor landscape" --tier final --dry-run
# この依頼の課金について承認を得た後のみ:
.venv/bin/python skills/image-generation/scripts/generate_image.py "A watercolor landscape" --tier final --confirm-spend
```

`--dry-run` は画像を生成しませんが、モデル一覧の取得で API に問い合わせることがあります。表示される `cost_usd_est` は画像出力分の概算であり、入力分や料金改定を含む請求額ではありません。[公式料金表](https://ai.google.dev/gemini-api/docs/pricing)を確認してください。`ultra` には追加で `--confirm-ultra` が必要です。詳しいワークフローと引数は [`SKILL.md`](skills/image-generation/SKILL.md) を参照してください。

API を使わない静的テストは `python3 -m unittest discover -s tests` で実行できます。

このリポジトリは [MIT License](LICENSE) です。
