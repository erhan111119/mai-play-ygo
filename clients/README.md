# clients/ —— 随插件自带的两个虚拟客户端

「麦麦玩游戏王」把**对局内核**与**出牌大脑**都放在这里，所以插件目录整个拷到别的机器上
也能直接开打（默认配置就是指向这两个目录，见 `config.toml` 的 `[paths]`）。

```
clients/
├── ygopro/          # 对局内核（服务端模式构建），一进程一房间
│   ├── ygopro.exe   #   必须是用「服务端模式」构建的版本（会自己报出监听端口）
│   ├── cards.cdb    #   卡库（WindBot 也靠它认牌；来自 mycard/ygopro-database）
│   ├── strings.conf #   内核文案（与内核同源）
│   ├── lflist.conf  #   禁限卡表
│   ├── script/      #   卡牌效果脚本（约 1.4 万个 c<卡号>.lua，来自 mycard/ygopro-scripts）
│   ├── expansions/  #   先行卡包（ygopro-super-pre.ypk 等）
│   └── replay/      #   对局录像落这里（运行期产物）
└── windbot/         # 出牌大脑（WindBot 的 Release 构建，含本插件定制的各卡组执行器）
    ├── WindBot.exe  #   编译自 windbot-src（MIT）
    ├── x64/sqlite3.dll  # 64 位进程要它才能读 cards.cdb
    ├── Decks/       #   内置卡组的 .ydk（插件会把它们登记进卡组池；也会往里写投稿卡组的 AI_*.ydk）
    ├── Dialogs/     #   WindBot 台词包（默认不启用）
    └── LICENSE      #   WindBot 的 MIT 许可（Copyright (c) 2015-2017 IceYGO）
```

## 怎么刷新 / 换掉这份客户端

```bash
# 从另一套本机环境里同步（源目录要自己给：--from <你的 ygopro 环境>）：
python tools/setup_clients.py --from <你的 ygopro 环境>
# 只检查现有这份是否齐（不写任何文件）：
python tools/setup_clients.py --check-only
```

`clients/` 里**没有**两样东西：

* **卡图**（`card_art_dir` 默认指向 `clients/art/`，但那份不进版本库——太大且版权归属不明）。
  想让 `/查房` 出图上带卡面，把 `paths.card_art_dir` 指到你客户端自带的卡图目录即可
  （例如 MDPro3 的 `Picture/Art` + `Picture/Closeup`）；没有卡图时图里画的是卡名框，功能不受影响。
* **WindBot 源码树**（约 250 MB）：只有两种用法需要它——给投稿卡组**生成**专属出牌脚本、
  以及编译**计划感知执行器**（`duel.ai_plan_coach`）。要用的时侯把 `paths.windbot_src_dir`
  指过去即可；不带它，插件就把投稿卡组交给通用脚本打。

## 许可与来源（**再分发前请读**）

| 组件 | 许可 | 来源 |
|---|---|---|
| `ygopro.exe`、`lflist.conf` | GPL-2.0 | https://github.com/mycard/ygopro （源码同仓库） |
| `script/` | GPL-2.0 | https://github.com/mycard/ygopro-scripts |
| `cards.cdb`、`strings.conf` | 上游未声明许可 | https://code.moenext.com/mycode/ygopro-database （镜像：github.com/mycard/ygopro-database） |
| `expansions/*.ypk` | 上游未声明许可 | https://github.com/ElderLich/TransSuperpre |
| `WindBot.exe`、`Decks/` | MIT | https://github.com/IceYGO/windbot （见 `windbot/LICENSE`） |

GPL-2.0 的两个组件再分发时需要随附许可文本与对应源码的获取方式；`cards.cdb` 这类卡牌数据
上游没有写明许可，**如果你要公开发布这个插件，请自行判断是否把 `cards.cdb` / `expansions/`
也放进仓库**（把它们从版本库里排除、让使用者用 `tools/setup_clients.py` 或
`tools/update_card_data.py` 自己补，同样能跑）。
