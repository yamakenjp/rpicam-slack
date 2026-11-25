rpicam-slack
============

Raspberry Pi のカメラ (rpicam-still) で、撮影のたびに露出を測って合わせ、Slack に投稿するツール。

  - 日の出・日の入りの時刻には頼らない。撮影のたびにテスト撮影 (1 回以上) で明るさを測り、
    露出 (シャッター・ゲイン) を自動で決めてから本撮影をして投稿する。
  - テストが 1 回で済めば 1 回。暗くて定まらない場合は、上限の回数まで測り直す。
  - 前回の露出を覚えていて、そこから測り始めるので、普段はたいてい 1 回で収束する。
  - カメラのタイムアウトは固定値ではなく、シャッター時間から計算する (後述)。
  - Slack の設定ファイル (.slack_option) は従来のものがそのまま使える。

旧 rpicam-still-to-slack の作り直しで、設定の形式は異なる (Slack 以外)。


動作の流れ (1 回の実行)
-----------------------

  ロック → テスト撮影 (1 回以上) → 本撮影 → スプールに保存 → Slack に投稿 → 失敗分の再送

 1. テスト撮影
    - 前回の露出が 60 分以内なら、その露出で撮る。そうでなければカメラの自動露出で撮る。
    - 小さい画像 (640x360) の平均輝度を測り、目標 (target_luma = 115) との差が
      tolerance (12) 以内なら収束。外れていれば、必要な露出を計算して撮り直す。
    - テスト撮影は速さ優先 (短いシャッター + 高いゲイン)。同じ「シャッター x ゲイン」なら
      同じ明るさになるので、短いテストで長い本撮影の明るさを予測できる。
    - 最大 max_tests (4) 回。露出がこれ以上変えられない (ハードウェアの限界) ときは打ち切る。
 2. 本撮影
    - 測った結果から露出を決める。ノイズが少ない順に、ゲイン 1.0 → ゲイン 2.0 → シャッターを
      max_shutter_s (12 秒) まで延ばす → ゲインを max_gain (16) まで上げる。
    - 持ち時間 (後述) が足りなければ、シャッターを短くしてゲインで補う。
      (撮れないより、少しノイズの多い写真が届くほうがよい)
 3. 投稿
    - 投稿前に JPEG と再送情報をスプールに保存し、成功したら削除する。
    - 既定で最大 3 回試す。レート制限 (HTTP 429) で諦めた場合は、Retry-After が示す時刻を状態に
      保存する (最長 1 時間)。以後の回は、その時刻まで投稿も警告も Slack に送らない。
      トークン不正・チャンネル未参加など再試行しても直らないエラーは、すぐに諦める。
    - 次回の投稿が成功したら、再送待ちを最大 3 枚送る。持ち時間の残りが、その回の投稿にかかった
      時間の 2 倍 (最低 5 秒) より少なければ、再送は次回に回す。読み取れない画像は保留して後続に進む。
      Slack が形式・サイズを理由に拒否した画像と空のファイルは、再送の対象から外す。
    - スプールに残った片割れのファイルは 1 日たつと、再送の対象から外した画像は外してから 1 日たつと、
      撮影時に削除する (外した画像は、スプールが上限を超えるときにも古いものから削除する)。
    - Slack が受理してからスプールを消すまでにプロセスが止まるか、応答を待つ間に持ち時間が
      切れると、同じ画像が再送されることがある。
 4. 失敗の通知
    - 撮影か投稿が alert_after_failures (2) 回連続で失敗したら、Slack に警告を投稿する
      (60 分に 1 回まで)。復旧したら一度だけ知らせる。
    - 失敗が持ち時間を使い切っていても (投稿が進まないときなど)、警告の投稿には 5 秒を与える。
      その回は持ち時間を最大 5 秒超える。
    - 状態は `rpicam-slack status` でも見られる。


カメラのタイムアウトについて
----------------------------

旧スクリプトでは、12 秒露光の本撮影が 45 秒のタイムアウトに間に合わず、夜間の投稿が止まる
事故があった (実測で約 53 秒かかる)。長時間露光は 1 枚の撮影に何フレームも必要なため、
「シャッター + 数秒」では足りない。

このツールでは 1 回の実行に持ち時間 ([budget] max_total_s、既定 110 秒) を決め、
カメラの各タイムアウトをそこから計算する。

  - 1 枚の所要時間の見積り = frames_per_shot x シャッター + プレビュー + 余裕
  - 実際にかかった時間から frames_per_shot を学習し、次回以降の見積りに反映する。
  - 持ち時間が足りないときは、本撮影を短いシャッターに切り替えて、必ず投稿まで進める。


必要なもの
----------

  - Raspberry Pi OS (Debian 13 trixie) と rpicam-apps (rpicam-still)。
  - Python 3.11 以上 (trixie は 3.13)。
  - 実行時の依存: slack-sdk と Pillow (pip で入る)。
  - 任意: ExifTool (撮影条件を EXIF に書く。なくても動く)。
  - Slack の Bot トークン (files:write と chat:write の権限。投稿先チャンネルに参加させておく)。


セットアップ
------------

  cd ~
  git clone <このリポジトリ> rpicam-slack && cd rpicam-slack
  sudo apt install -y python3-venv libimage-exiftool-perl
  python3 -m venv .venv
  .venv/bin/pip install .

  # 設定
  mkdir -p ~/.config/rpicam-slack
  cp config/camera.example.toml        ~/.config/rpicam-slack/camera.toml
  cp config/camera.local.example.toml  ~/.config/rpicam-slack/camera.local.toml   # 機体ごとの値
  # Slack は従来の .slack_option をそのまま使える:
  install -m 600 ~/rpicam-still-to-slack/.slack_option ~/.config/rpicam-slack/slack_option

  # 確認と調整
  .venv/bin/rpicam-slack check            # 設定・権限・コマンドの有無を確認
  .venv/bin/rpicam-slack focus-scan       # 初回だけ。出た lens_position を camera.local.toml に書く
  .venv/bin/rpicam-slack capture --no-upload --keep /tmp/test.jpg    # 投稿せずに撮って画像を確かめる
  .venv/bin/rpicam-slack capture          # 実際に 1 枚投稿する

  # 15 分おきの自動実行 (旧タイマーを止めて切り替える)
  sudo systemctl disable --now rpicam-still-to-slack.timer
  sudo cp systemd/rpicam-slack.service systemd/rpicam-slack.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now rpicam-slack.timer

systemd のユニットは /home/pi/rpicam-slack に置く前提。場所が違うなら WorkingDirectory と
ExecStart を直す。


コマンド
--------

  rpicam-slack capture [--no-upload] [--dry-run] [--keep PATH]
      測って、撮って、投稿する。--dry-run は最初のカメラコマンドを表示するだけ。
  rpicam-slack focus-scan [--start 0] [--end 8] [--step 0.5]
      レンズ位置を振って撮り、いちばん鮮明な位置 (FocusFoM が最大) を推奨する。
  rpicam-slack status
      最後の成功、連続失敗の回数、直近の露出、再送待ちの枚数。
  rpicam-slack check
      設定の検証、rpicam-still / slack-sdk / ExifTool の有無、Slack 設定の権限。
  rpicam-slack show-config
      実際に使われる設定 (既定値とファイルを重ねた結果) を表示する。

共通オプション: --config FILE (繰り返して重ねられる)、--slack-option FILE、--debug

終了コード: 0 成功 / 1 撮影・スキャン失敗 / 2 設定エラー
            3 投稿失敗 (再送用の画像なし) / 75 投稿を延期 (画像は保存済み)
spool_max_files = 0 でスプールを無効にした場合や、保存先が満杯で退避できなかった場合、
投稿にも失敗すると 3 を返し、systemd でも失敗扱いになる。


設定
----

カメラの設定は TOML。次の順に読んで重ねる (後ろが勝つ)。

  1. ~/.config/rpicam-slack/camera.toml        全カメラ共通 (Ansible などで配る)
  2. ~/.config/rpicam-slack/camera.local.toml  機体ごと (レンズ位置など)

環境変数 RPICAM_SLACK_CONFIG にパスを ":" 区切りで並べると、そちらが優先される。
未知のキーはエラーになる (打ち間違い、今は使われないキーを黙って無視しないため)。
全項目と既定値は config/camera.example.toml を参照。

Slack の設定 (旧 .slack_option と同じ。KEY=VALUE 形式、権限は 600)

  SLACK_TOKEN (または SLACK_BOT_TOKEN)   Bot トークン
  CHANNEL     (または SLACK_CHANNEL_ID)  投稿先チャンネル ID
  COMMENT_TEMPLATE                       投稿のコメント。使えるフィールド:
      {timestamp} {profile}                  旧スクリプトと同じ
      {camera} {shutter} {gain} {luma} {tests} {lux}   新規

{profile} は露出から決まる day / twilight / night (旧スクリプトの日中・夕方・夜に相当)。


旧設定からの対応
----------------

  旧 (.camera_option)                          新 (camera.toml)
  -------------------------------------------  -------------------------------------
  WIDTH / HEIGHT / QUALITY                     [camera] width / height / quality
  HDR_MODE / METERING                          [camera] hdr / metering
  LENS_POSITION                                [camera] lens_position (camera.local.toml)
  AUTO_SHUTTER_MAX_US                          [exposure] max_shutter_s (秒)
  AUTO_SHUTTER_TARGET_GAIN                     [exposure] preferred_gain
  NIGHT_EV など *_EV                           [exposure] ev (全体の補正)
  TIMEOUT_MS / COMMAND_TIMEOUT_S               なし (シャッター時間から自動計算)
  DAY_* / TWILIGHT_* / NIGHT_* の固定値        なし (測って決める)
  LUX_* のしきい値                             なし (画像の明るさで判断する)

cam2 のように夜間に長い露出が必要な機体は、camera.local.toml で [exposure] max_shutter_s と ev を調整する。


複数台の運用
------------

  - camera.toml は全台同じファイル、camera.local.toml だけ機体ごとに違う。
    Ansible で camera.toml を配り、camera.local.toml は host_vars から生成するとよい。
  - 状態 (前回の露出、失敗回数) は ~/.local/state/rpicam-slack/state.json。
  - 同時実行はロックで防ぐ。前の実行が 30 秒待っても終わらなければ、次は静かにスキップする。
    focus-scan は 1 枚ごとにロックを取り直すので、スキャン中に定時撮影の時刻が来ても、
    撮影はスキャンの合間に行われる。


テスト
------

  python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
  .venv/bin/ruff check . && .venv/bin/ruff format --check .
  .venv/bin/python -m pytest

テストはカメラの代わりに「明るさだけが 'シャッター x ゲイン' で決まるシーン」のシミュレータと、
偽の rpicam-still を使う。各テストは、撮影が持ち時間とタイムアウトを守ることも検証する。


未検証・既知の制限
------------------

  * 実機 (Pi Zero 2 W / imx708) ではまだ動かしていない。テストはシミュレータと偽の
    rpicam-still で行った。最初は必ず `capture --no-upload --keep /tmp/test.jpg` で画像を確かめること。
  * rpicam-still のオプション (--immediate / --gain / --autofocus-on-capture など) は
    rpicam-apps の仕様に基づくが、バージョン差で引数が通らないことがありうる。
    その場合は `rpicam-slack capture --debug` が実際のコマンドと rpicam-still の出力を表示する。
  * 露出の計算は「平均輝度 ~ 露出 ^ (1/gamma)」(gamma = 2.0) の近似。実機の応答とずれても、
    測り直しで収束する。収束が遅い場合は [exposure] gamma を調整する。
  * 画像全体の平均輝度で判断する。夜に強い街灯が写るシーンでは、白飛びの割合が多いときは
    それ以上明るくしない (max_clipped_fraction)。
  * 連続失敗の警告は、その機体が動いている間だけ出せる。機体自体が止まった場合は検知できない
    (Slack のチャンネルに投稿が来なくなる、で気づくことになる)。
