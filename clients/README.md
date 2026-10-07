# clients/ —— 随插件自带的两个虚拟客户端

「麦麦玩游戏王」把**对局内核**与**出牌大脑**都放在这里，所以插件目录整个拷到别的机器上
也能直接开打（默认配置就是指向这两个目录，见 `config.toml` 的 `[paths]`，模板是 `config.toml.example`）。

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
* **WindBot 源码树**（约 250 MB）：只有想自己（或让 agent）**改/加执行器**时才需要它——
  在源码树里编译出一份带定制执行器的 `WindBot.exe`，玩法见 `executors/README.md`。
  把 `paths.windbot_src_dir` 指过去即可；不带它，插件就用 `clients/windbot/WindBot.exe` 打。

## 许可与来源（**再分发前请读**）

| 组件 | 许可 | 来源 |
|---|---|---|
| `ygopro.exe`、`lflist.conf` | GPL-2.0 | https://github.com/mycard/ygopro （源码同仓库） |
| `script/` | GPL-2.0 | https://github.com/mycard/ygopro-scripts |
| `cards.cdb`、`strings.conf` | 上游未声明许可 | https://code.moenext.com/mycode/ygopro-database （镜像：github.com/mycard/ygopro-database） |
| `expansions/*.ypk` | 上游未声明许可 | https://github.com/ElderLich/TransSuperpre |
| `WindBot.exe`、`Decks/` | MIT | https://github.com/IceYGO/windbot （见 `windbot/LICENSE`） |

GPL-2.0 的两个组件再分发时需要随附许可文本与对应源码的获取方式：

* `ygopro.exe` 是**上游 `mycard/ygopro` 源码、用「服务端模式」开关编译的构建，源码本身没有改动**——
  对应源码与许可文本见上表链接（`mycard/ygopro` 仓库内即含 GPL-2.0 文本）。
* `script/` 下的卡牌脚本逐文件来自 `mycard/ygopro-scripts` 与 `ElderLich/TransSuperpre`
  （后者见 `expansions/`），同样按 GPL-2.0 分发。
* `WindBot.exe` 与 `Decks/` 是 MIT：许可文本随包放在 `windbot/LICENSE`；本插件在这份构建里加的
  各卡组执行器（`executors/`）与宿主侧补丁也一并公开在同一个仓库里。

上游**未声明许可**的只有 `cards.cdb` / `strings.conf` / `expansions/` / 卡图这几样（表中已逐项标注来源）。
这类数据在 ygopro 生态里长期公开流通、各家客户端都在分发，本插件的处置是：

* **仓库与 Release 原样随包**——不主张任何权利、不单独收费，只为了让插件开箱即用；
* **权利方要求即删**：在仓库开个 Issue 说一声，我们会立刻把对应文件从仓库与 Release 里剔除（不要求举证）；
* **不想带这几样的部署者**：删掉 `clients/ygopro/cards.cdb` 与 `clients/ygopro/expansions/`，
  让使用者跑 `python tools/setup_clients.py --from <自己的 ygopro 环境>` 从本机客户端补回来，
  或把 `[paths]` 直接指向自己客户端的同名文件（这两种做法都不需要改动插件代码）。

完整的发行结论（含三类内容的处置表）在根目录 [README](../README.md) 的「许可与随包内容」一节。
