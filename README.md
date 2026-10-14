# Simultaneous-Interpretation · 法语 → 中文实时同传

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)

把电脑正在播放的声音（网课、视频会议、视频网站）实时识别成法语，翻译成中文，显示在一个浮在所有窗口上面的透明字幕窗里。

- **边说边译**：话还没说完，中文就跟着出来，看到法语之后大约 1 秒就能看到中文；
- **不影响你听**：只是“复制”一份系统声音，戴耳机、用扬声器都照常能听到原声；
- **事后能校对**：每次的原文、译文和一份录音都会保存下来，记录里标着每句话在录音里的位置；
- **控制面板**：开始 / 暂停监听，换语言（法、英、西、德、意、葡语 → 中文或其他语言）；
- **字幕窗**：可以往回翻看、收起成小条、拖边改大小、直接调字号。

![悬浮字幕窗（示例文字）](docs/overlay_preview.png)

![控制面板](docs/panel_preview.png)

> 这是一个开源的个人项目，目前只在作者自己的 Mac 上长期使用过。安装要用到终端（命令行），下面一步一步写清楚了，照着复制粘贴就行。

**目录**：[能不能用](#先看看你的电脑能不能用) · [安装](#安装macos) · [使用](#使用) · [常见问题](#常见问题) · [更新和卸载](#更新和卸载) · [Windows](#windows还没在真机上验证过) · [工作原理和开发](#工作原理)

---

## 先看看你的电脑能不能用

| 需要 | 说明 |
|---|---|
| **Apple 芯片的 Mac**（M1、M2、M3、M4、M5……） | 语音识别用的 MLX 只支持 Apple 芯片。查看方法：左上角苹果菜单 →“关于本机”，“芯片”一栏写着 Apple M 开头的就可以。Intel 芯片的 Mac 用不了。 |
| **macOS 14.2 或更新** | 录系统声音用的接口是 macOS 14.2 才有的。作者在 macOS 26 上使用，更早的版本没有测试过。 |
| **大约 5 GB 硬盘空间** | 开发工具约 1～2 GB，程序的运行环境约 1.6 GB，识别模型约 1.6 GB。 |
| **网络** | 安装时要下载约 2～3 GB；使用时，翻译要联网（识别在本机完成，不联网）。 |
| **翻译服务的 API 密钥**（可选） | 默认用 DeepSeek，按用量付费，需要自己注册、充值。不填密钥也能用，但只显示法语原文、没有中文。 |

Windows 版的代码写好了，但还没在真机上运行过，见[最后一节](#windows还没在真机上验证过)。

---

## 安装（macOS）

第一次安装大约 20～40 分钟，大部分时间在等下载。整个过程只需要做一次。

### 第 0 步：打开“终端”

按 ⌘ + 空格，输入“终端”（或 Terminal），回车。会打开一个可以输入命令的窗口。

下面每个灰色方框里是一条命令：**复制，粘贴到终端里，按回车**，等它运行完（重新出现 `%` 提示符）再做下一条。粘贴进去的命令如果要求输入密码，输入的是你登录 Mac 的密码；输入时屏幕上不显示任何字符，这是正常的，输完按回车即可。

### 第 1 步：安装 Xcode 命令行工具

里面有下载项目要用的 git，以及编译“录系统声音的小程序”要用的 Swift 编译器（不需要安装完整的 Xcode）。

```bash
xcode-select --install
```

会弹出一个窗口，点“安装”，同意协议，等它装完（5～15 分钟）。如果终端提示 `command line tools are already installed`，说明已经装过了，直接下一步。

### 第 2 步：安装 Homebrew

Homebrew 是 Mac 上常用的软件安装工具，下一步要用它装 Python。

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

按提示输入密码、按回车。**装完以后看终端最后几行的“Next steps”**：它会让你再运行两三条命令，把 brew 加进环境。在 Apple 芯片的 Mac 上一般是这两条（以终端里实际显示的为准）：

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
```

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
```

检查一下：

```bash
brew --version
```

能显示版本号（比如 `Homebrew 4.x.x`）就成功了。已经装过 Homebrew 的话，这一步跳过。

### 第 3 步：安装 Python 3.12（和 ffmpeg）

```bash
brew install python@3.12 ffmpeg
```

- Python 是这个程序的运行环境。Mac 自带的 Python 版本太老（3.9），要另装一个 3.11 以上的，这里用 3.12。
- ffmpeg 是可选的：只有想用 mp3、m4a 这类音频文件来测试时才需要，日常同传用不到。

检查一下：

```bash
python3.12 --version
```

显示 `Python 3.12.x` 就对了。

### 第 4 步：下载这个项目

下面把项目下载到你的个人文件夹（访达里“前往 → 个人”，快捷键 ⇧⌘H）里，之后的命令都在这个文件夹里运行。

```bash
cd ~
```

```bash
git clone https://github.com/YEKESONG/Simultaneous-Interpretation.git
```

```bash
cd ~/Simultaneous-Interpretation
```

> 以后每次打开新的终端窗口，想运行本项目的命令前，都要先运行一次 `cd ~/Simultaneous-Interpretation` 进入项目文件夹。

### 第 5 步：建立项目自己的 Python 环境，安装依赖

“虚拟环境”是放在项目文件夹里的一个独立的 Python（文件夹名叫 `.venv`），装的东西不会影响电脑上的其他软件，删掉项目文件夹就一起删掉了。

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

第二条要下载几百 MB，网络好的话一两分钟，最后出现 `Successfully installed ...` 就成功了。装完后可以跑一遍自带的测试，确认环境没问题：

```bash
.venv/bin/python -m pytest
```

最后一行显示 `127 passed`（数字以后可能会变多）、没有 `failed` 字样，就是全部通过。

### 第 6 步：下载语音识别模型（约 1.6 GB）

程序在本机识别语音，用的是 OpenAI 开源的 Whisper 模型（large-v3-turbo，适配 Apple 芯片的版本）。模型比较大，程序不会自动下载，用下面这条命令下载：

```bash
.venv/bin/hf download mlx-community/whisper-large-v3-turbo
```

下载完显示一个文件夹路径就成功了（模型存在 `~/.cache/huggingface/` 里）。中途出现 `unauthenticated requests` 的提示可以不管。

> 在中国大陆下载很慢或连不上时，可以通过镜像站下载，改用这条命令：
> `HF_ENDPOINT=https://hf-mirror.com .venv/bin/hf download mlx-community/whisper-large-v3-turbo`

### 第 7 步：填翻译服务的 API 密钥

翻译是把识别出来的法语发给大模型（默认 DeepSeek）翻成中文。需要一个 API 密钥：

1. 打开 [DeepSeek 开放平台](https://platform.deepseek.com)，注册并登录；
2. 在“API keys”页面创建一个密钥。它是一串以 `sk-` 开头的字符，**只显示一次**，马上复制下来；
3. 在平台上充一点钱（按用量计费，费用以平台上公布的价格为准）。

然后把密钥存进项目文件夹里一个叫 `.env` 的文件：

```bash
cp .env.example .env
```

```bash
open -e .env
```

会用“文本编辑”打开这个文件。把 `DEEPSEEK_API_KEY=` 这一行改成 `DEEPSEEK_API_KEY=sk-你的密钥`（等号两边不要加空格和引号），按 ⌘S 保存，关掉窗口。

- `.env` 只存在你自己的电脑上，不会被上传；不要把密钥发给别人或贴到网上。
- 不想用翻译、或者暂时没有密钥：这一步可以跳过，程序照样运行，只显示法语原文。
- 想换成别的翻译服务（任何兼容 OpenAI 接口的都可以，包括本机运行的 Ollama），见[常见问题](#想换别的翻译服务或者完全离线)。

### 第 8 步：试一下录系统声音，并允许权限

先在浏览器里随便放一个有声音的视频，然后运行：

```bash
.venv/bin/python -m simul_interp record --seconds 10
```

- 第一次运行会先编译一个录系统声音的小程序 `si-audio-tap`（几秒钟）；
- macOS 会弹窗询问是否允许录制系统音频，点**允许**；
- 接下来终端里每秒出现一行音量条和音量数值（dBFS，越接近 0 声音越大），10 秒后结束，录音存在项目的 `recordings/` 文件夹里。注意要一直有声音在放：电脑完全不出声的时候系统不送数据，计时也会停住。

如果音量条一直是最低（-120 dBFS 左右）、或者提示“整段都是静音”“一直收到全零的静音”：打开 **系统设置 → 隐私与安全性 → 屏幕与系统录音**，在“仅系统录音”里找到 `si-audio-tap`，把开关打开，再运行一次上面的命令。

### 第 9 步：启动

```bash
.venv/bin/python -m simul_interp
```

几秒后会出现控制面板和字幕窗（第一次加载识别模型要多等一会儿）。打开一个法语视频试试吧。

以后不用再敲命令：在访达里打开项目文件夹（个人文件夹里的 `Simultaneous-Interpretation`），**双击 `启动同传.command`** 就能启动。启动时会开一个终端窗口显示运行日志，这个窗口要一直开着，关掉它同传也就退出了。

---

## 使用

### 控制面板

- **选语言**：左边是说话人说的语言（法、英、西、德、意、葡语），右边是译成的语言。运行中随时可以换，从下一句话开始生效，会话记录里会留一行说明。这里选的只管这一次；想改每次启动的默认值，见[配置文件](#配置文件)。
- **开始 / 暂停监听**：暂停时不识别、不翻译，录音也跟着停；继续后记录里留一行“暂停后从这里继续”。
- **字幕窗**：可以隐藏、收起 / 展开。
- **记录和录音**：最下面显示这次的录音和记录的文件名，点按钮打开它们所在的文件夹。
- 点面板窗口的关闭按钮只是把面板藏起来，同传照常进行。要找回它：右键字幕窗，或者点屏幕顶部菜单栏里的“译”字图标，选“控制面板”。**要结束程序，点面板上的“退出”。**

### 字幕窗

- **看**：底部是正在说的这句话：上面一行是法语（白色是已确认的，灰色是还可能变的），下面一行是到目前为止的中文草稿（颜色暗一点，会随着话往下说而更新）。一句话说完后换成正式译文，滑到上面的历史里，越旧越暗。
- **往回翻**：在字幕窗上滚动鼠标滚轮或者触控板双指滑动，或者拖右边的滑动条。回看时新句子照常加在下面，眼前的内容不动；滚回最底下或点顶部的“回到最新”，就继续跟着最新的句子。
- **移动**：按住字幕窗中间拖动。
- **改大小**：鼠标移到字幕窗上会显示边框，拖任意一条边或一个角（右下角有拉手）。文字会按新的大小重新排。
- **调字号**：鼠标移到字幕窗上，右上角会出现 **A− / A+**；也可以按住 ⌘ 滚动滚轮，或者在触控板上双指捏合。
- **收起**：点右上角的“–”，字幕窗缩成一个小条，点小条再展开。收起时照常识别、翻译、记录。
- **右上角的小圆点**变绿，表示正在听到有人说话。
- **右键菜单**：调背景深浅、鼠标穿透（点击会直接落到字幕窗下面的窗口）、清空字幕、重新连接音频设备、退出。开了鼠标穿透以后，用菜单栏的“译”字图标关掉它。
- 位置、大小、字号、背景深浅都会自动保存。

### 会话记录和录音

每次同传会在项目文件夹里留下两个同名的文件，方便对照着校对：

- `transcripts/同传_日期_时间_译文.md`：原文和译文，每段前面的 `[12:34]` 是这段话在录音里的位置（一般比这段话的开头早不到一秒），把播放器拖到这里就能听到这段话；
- `recordings/同传_日期_时间.wav`：录下的系统声音，每小时约 115 MB。只录“电脑在出声”的时间段，没有任何声音时不录。

保存位置可以在[配置文件](#配置文件)里改。只想这一次不录音：启动时加 `--no-recording`（见下面的命令表）。**录下别人的发言之前，请先征得对方同意。**

### 常用命令

先 `cd ~/Simultaneous-Interpretation` 进入项目文件夹，再运行：

| 命令 | 作用 |
|---|---|
| `.venv/bin/python -m simul_interp` | 启动（和双击 `启动同传.command` 一样） |
| `.venv/bin/python -m simul_interp run --paused` | 启动后先不监听，等在控制面板上点“开始监听” |
| `.venv/bin/python -m simul_interp run --no-recording` | 这一次不保存录音 |
| `.venv/bin/python -m simul_interp run --no-translate` | 只识别，不翻译 |
| `.venv/bin/python -m simul_interp run --ui console` | 不要字幕窗，直接在终端里显示 |
| `.venv/bin/python -m simul_interp run --file 某个音频.mp3` | 用音频文件代替系统声音（测试用；非 WAV 格式需要 ffmpeg） |
| `.venv/bin/python -m simul_interp record --seconds 10` | 录 10 秒系统声音，检查内录是否正常 |
| `.venv/bin/python -m simul_interp config` | 显示现在生效的全部配置 |
| `.venv/bin/python -m simul_interp --set asr.language=en` | 临时改一项配置（这里是改成识别英语），只管这一次 |
| `.venv/bin/python -m simul_interp --help` | 查看所有命令和参数 |

运行中按 Ctrl + C 也能退出。

### 配置文件

不改配置也能用。想改默认设置时，先复制一份示例配置：

```bash
cp config.example.toml config.toml
```

```bash
open -e config.toml
```

文件里每一项后面都有中文说明，改完按 ⌘S 保存，下次启动生效。常改的几项：

| 位置 | 作用 |
|---|---|
| `[asr]` 里的 `language = "fr"` | 默认识别的语言：fr 法语、en 英语、es 西班牙语、de 德语、it 意大利语、pt 葡萄牙语 |
| `[translate]` 里的 `target_language = "简体中文"` | 译成什么语言 |
| `[translate]` 里的 `glossary` | 术语表：去掉行首的 `#`，按例子写，比如 `{ "TP" = "实验课" }` |
| `[translate]` 里的 `live_draft = true` | 边说边译；改成 `false` 就只显示定稿的译文（要等整句说完，但不会先错后改） |
| `[transcript]` 里的 `dir = "transcripts"` | 会话记录存在哪里，可以写完整路径，比如 `"/Users/你的用户名/Documents/同传记录"` |
| `[recording]` 里的 `dir`、`enabled` | 录音存在哪里、要不要录 |
| `[ui]` 里的 `autostart = true` | 改成 `false`：每次启动先不监听 |

注意：

- 用“文本编辑”改配置前，先在菜单 **编辑 → 替换** 里关掉“智能引号”，否则英文引号 `"` 会被自动换成中文弯引号，程序就读不了这个文件了。
- 写错了的话，启动时会在终端里报错并指出是哪一项。改乱了可以删掉 `config.toml`，程序会回到默认设置。

---

## 常见问题

### 安装时

- **`zsh: command not found: brew`**：Homebrew 装完后没有做“Next steps”。回到[第 2 步](#第-2-步安装-homebrew)，运行那两条 `eval` / `echo` 命令。
- **`zsh: command not found: python3.12`**：第 3 步没成功，重新运行 `brew install python@3.12`。
- **`pip install` 报错找不到 `mlx` 之类的包**：多半是 Intel 芯片的 Mac（不支持），或者 Python 版本不对。运行 `.venv/bin/python --version` 看看是不是 3.11 以上；不对的话删掉 `.venv` 文件夹（`rm -rf .venv`），从第 5 步重来。
- **在中国大陆下载很慢**：装依赖时可以用清华的镜像：`.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`；下载模型见第 6 步的镜像写法。
- **启动时报错“本地没有找到识别模型”**：第 6 步没做完，按错误信息里给出的命令下载。

### 使用时

- **字幕窗一直没有字**：先看右上角的小圆点在放视频时会不会变绿。不会的话，多半是没有系统录音权限，按[第 8 步](#第-8-步试一下录系统声音并允许权限)最后一段打开权限。字幕窗左上角的黄色小字也会提示出了什么问题。
- **只有法语，没有中文**：没填密钥、密钥填错了，或者 DeepSeek 账户没有余额。启动时终端里会有提示（比如“没有设置翻译 API 密钥”“密钥无效”）。改完 `.env` 要重新启动程序。
- **找不到控制面板了**：点屏幕顶部菜单栏里的“译”字图标，或者右键字幕窗，选“控制面板”。
- **换了耳机或蓝牙音箱以后没声音了**：macOS 上会自动重新连接；不行的话右键字幕窗，选“重新连接音频设备”。
- **中文偶尔先错后改**：这是边说边译的特点：草稿照着还没确认的法语先翻，识别改了它也跟着改，一两秒后就稳定了。不喜欢的话在配置里设 `live_draft = false`。
- **电脑发热、越来越慢**：长时间识别会让没有风扇的 MacBook Air 发热降频，字幕会慢一些。可以垫高散热，或者不用时在控制面板上暂停。
- **翻译费用**：说话的时候大约每秒发一次翻译请求（边说边译 + 提前翻译）。在意费用的话，把配置里的 `live_draft` 和 `speculative` 改成 `false`，请求会少很多，代价是中文出得晚一些。

### 想换别的翻译服务，或者完全离线

翻译部分兼容 OpenAI 的接口格式，在 `config.toml` 的 `[translate]` 里改四项就能换：

```toml
base_url = "https://服务商的接口地址/v1"
model = "模型名"
api_key_env = "MY_API_KEY"   # 然后在 .env 里写 MY_API_KEY=你的密钥
extra_body = {}              # 这一项是给 DeepSeek 关“思考模式”用的，换别家时改成空的
```

想完全离线：在本机装 [Ollama](https://ollama.com) 并下载一个翻译效果好的模型，然后按 `config.example.toml` 里“本地 Ollama”那段示例填写（不需要密钥）。本机跑大模型对电脑要求高，翻译会比云端慢。

---

## 更新和卸载

**更新到最新版**：

```bash
cd ~/Simultaneous-Interpretation
```

```bash
git pull
```

```bash
.venv/bin/pip install -r requirements.txt
```

你的 `.env`、`config.toml`、记录和录音不会被覆盖。

**卸载**：先把 `transcripts/`、`recordings/` 里想留的记录和录音挪走，然后：

```bash
rm -rf ~/Simultaneous-Interpretation
```

```bash
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo
```

第一条删除项目（包括它的 Python 环境），第二条删除识别模型。系统设置里的录音权限可以手动关掉。用 Homebrew 装的 Python 如果别的软件也不用，可以 `brew uninstall python@3.12`。

---

## Windows（还没在真机上验证过）

Windows 部分的代码写好了，GitHub 上的自动测试会在 Windows 虚拟机里安装依赖、跑测试，但**还没有人在真正的 Windows 电脑上用过**，可能会遇到问题。想尝试的话：

- 需要 Windows 10 / 11，最好有 NVIDIA 显卡（没有显卡的话，识别很难跟上说话的速度）。
- 录系统声音用的是 Windows 自带的接口，不需要虚拟声卡，也不需要授权。
- 安装：从 [python.org](https://www.python.org/downloads/) 装 Python 3.12（安装时勾选 “Add python.exe to PATH”），从 [git-scm.com](https://git-scm.com) 装 Git，然后在 PowerShell 里：

```powershell
git clone https://github.com/YEKESONG/Simultaneous-Interpretation.git
cd Simultaneous-Interpretation
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.toml config.toml
copy .env.example .env
notepad .env
```

- 识别用 faster-whisper（同样是 large-v3-turbo），第一次使用要下载约 1.6 GB：在 `config.toml` 的 `[asr]` 里把 `allow_download` 改成 `true`，程序启动时会自动下载。
- 用显卡加速需要按 [faster-whisper 的说明](https://github.com/SYSTRAN/faster-whisper#gpu) 安装 NVIDIA 的 CUDA 库。
- 启动：双击 `启动同传.bat`。换了耳机或音箱后，右键字幕窗选“重新连接音频设备”。

---

## 已知问题

- 声音在半句话处突然停止（比如暂停视频）时，最后半句可能被识别模型“补全”错。
- 没人说话、但一直有像人声的杂音（比如远处的交谈）时，识别模型偶尔会编出几句短句。最常见的 “Merci.” 已经拦下，别的还拦不住。
- 特别含糊、特别轻的一声 “Merci.” 会被当成杂音丢掉。
- 边说边译的草稿是照着还没确认的文字译的，识别模型先听错的词，草稿也跟着错，一两秒后改过来。
- 识别模型从半句话中间开始听（刚启动、暂停后继续）时，有时一段时间不加标点，正式译文会来得晚、一段很长（草稿不受影响）。
- 翻译请求比较多（见常见问题里的“翻译费用”）。
- 无风扇的 Mac 长时间满负荷识别会发热降频（实测约慢 40%）。
- Windows 部分还没有在真机上运行过。

---

## 工作原理

```
系统声音 ─► 语音检测 ─► 流式识别 ─► 按句切分 ─► 大模型翻译（流式）─► 透明字幕窗
   │           │            │                          │                  │
   │       Silero VAD   Whisper turbo           OpenAI 兼容 API        PySide6
   ├─ macOS：Core Audio Taps（自写的 Swift 小程序）
   └─ Windows：WASAPI loopback
```

- **录音**：只“复制”一份系统声音，不改变声音的去向，所以不需要虚拟声卡，也不会出现戴耳机听不到的问题。
- **识别**：Whisper 不是流式模型，这里用“连续两次识别结果一致才确认”的策略（LocalAgreement）实现边听边出字。
- **翻译**：边说边译。正在说的这句话每多出几个词，就把已经听到的部分先译一版草稿，屏幕上的中文紧跟着法语走；整句定下来（遇到句号，或者较长的分句）再出正式译文换掉草稿，会话记录里存的是正式译文。句子还没完全确认就先在后台翻好（投机翻译），确认时直接换上。说话人停下来想词时留下的半句话不急着定稿，等他接着说（最多等 1.5 秒）。
- **显示**：底部是高度固定的“当前句”区域，正在识别的原文和它的译文草稿都在里面实时更新；下一句开始时，这一对才平滑地滑进上方的历史。区域里怎么变都不会带动别的内容。

目前的延迟（M5 MacBook Air，Whisper turbo，法语合成语音，真实 DeepSeek API）：

- 一个法语词出现在屏幕上之后，**包含它的中文出现的中位数约 1.2～1.4 秒**（90% 在 2.1 秒以内）。没有边说边译时要等整句说完，中位数 2.1～2.3 秒，最慢的词要等 6～7 秒；
- 一句话说完后，屏幕上开始出现正式译文的中位数约 1.1～1.3 秒。

其中约 0.9 秒是翻译服务出第一个字的时间。各种配置的对比数据见 [docs/DEVLOG.md](docs/DEVLOG.md) 的 S9～S12 和 S21。

## 进度

- [x] S0 项目骨架与开发规范
- [x] S1 macOS 系统声音采集
- [x] S2 音频文件模拟源与测试音频
- [x] S3 语音检测与分段
- [x] S4 Whisper 流式识别
- [x] S5 流式翻译
- [x] S6 串起完整流程（终端版）
- [x] S7 透明悬浮字幕窗
- [x] S8 Windows 支持（代码和自动测试已完成，未在 Windows 真机上验证）
- [x] S9 延迟优化（测量工具、停顿时提前识别、量化选项、投机翻译）
- [x] S10 真实内录端到端验证；全程序统一高精度时钟
- [x] S11 默认 4 位量化；真实翻译 API 实测
- [x] S12 实时识别改用 turbo（对比实验后由用户决定）
- [x] S13 字幕窗改版：原文译文成对同步出现，不闪不跳
- [x] S13b 当前句区域：暂定文字只在预留的空白里更新，译文填进留白
- [x] S14 录音保存：内录时同步存一份 WAV，方便事后对照录音校对译文
- [x] S14b 会话记录里的时间改成录音里的位置
- [x] S15 杂音时段不再输出 “Merci.”（整句只有它时向识别模型核实）
- [x] S16 没说完的半句话先等一等：说话人停下来想词时不再把一句话切成两半
- [x] S17 字幕窗可以往回翻看：滚轮或右侧的滑动条，回看时眼前的内容不动
- [x] S18 字幕窗可以收起成一个小条，点一下再展开
- [x] S19 流水线支持暂停 / 继续，以及运行中换语言（法、英、西、德、意、葡 → 任意目标语言）
- [x] S20 应用前端：控制面板（换语言、开始 / 暂停监听、管理字幕窗），双击就能启动
- [x] S21 边说边译：话还没说完就出中文草稿，看到法语之后约 1 秒就能看到中文（原来要等整句说完）
- [x] S22 字幕窗任意边角都能拖动改大小，文字跟着重新排满；字号直接在字幕窗上调（A− / A+、⌘ + 滚轮、双指捏合）
- [x] S23 README 改成从零开始的安装和使用教程（在全新的环境里照着做了一遍）

## 开发

运行测试：

```bash
.venv/bin/python -m pytest
```

测量工具（先用 `scripts/make_test_audio.py` 生成测试音频，用的是 macOS 自带的合成语音）：

```bash
.venv/bin/python scripts/make_test_audio.py                # 生成法语测试音频
.venv/bin/python -m simul_interp bench-asr samples/fr_meeting.wav   # 流式识别的延迟和准确率
.venv/bin/python -m simul_interp bench-translate           # 翻译服务的首字延迟
.venv/bin/python -m simul_interp bench-pipeline samples/fr_meeting.wav --mock-translate  # 端到端：看到法语之后多久看到中文
.venv/bin/python -m simul_interp --set asr.quantize_bits=0 bench-asr samples/fr_meeting.wav  # --set 临时改配置做对比
.venv/bin/python scripts/compare_models.py 录音.wav --configs 模型A:4,模型B:0  # 离线比较几种模型配置的准确率和速度
```

参与开发的约定见 [CLAUDE.md](CLAUDE.md)（提交方式、不要提交的文件、Windows 上的验证顺序）。

### 怎么通过这个仓库学习

每完成一个小阶段就单独提交一次，提交历史本身就是一份搭建教程：

```bash
git log --reverse --stat
```

每个阶段遇到的问题、做法和验证方式记录在 [docs/DEVLOG.md](docs/DEVLOG.md)。
