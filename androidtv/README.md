# MediaFans TV

电视端。**它不是把网页端套个壳**——套壳解决不了电视上最要紧的那件事。

## 为什么要单独做一个

网页端在电视浏览器里能开，但会卡在解码上：网盘里画质最好的那一份往往是
原盘 MKV（HEVC 视频 + DTS-HD 音轨），**浏览器解不了，而且不报错**，
只会黑屏一直往下载。主项目 README 的「为什么有些文件只有原画」记着一个实测：
一个目录 40 个可播放文件，13 个被夸克标成了图片、压根没有转码档可退。

电视盒子有硬解。所以这个 app 的播放器**默认播原画**（`PlayInfo.pick`），
跟网页端优先挑转码档正好相反。

另外两件只有原生端能做好的：D-pad 焦点、遥控器的媒体键。

## 卡顿：默认播原画的代价

原盘的码率比网盘转码档高得多——实测同一集 1.82GB / 19.8 分钟 ≈ **12.2 Mbps**，
而它的 4K 转码档只有 6380 kbps，正好一半。电视多半挂 Wi-Fi，这个差距很要命。

三处一起处理：

- **缓冲加大**。ExoPlayer 默认那套是按「网速稳定的手机看转码流」调的：
  起播只等 2.5 秒、卡顿后只等 5 秒就恢复，缓冲还没垫起来又开始放，于是走走停停。
  改成缓冲目标 60 秒 / 上限 256MB，卡顿后等 8 秒再续播。代价是起播慢一点。
- **上下键切档**。⬆ 更清晰 / ⬇ 更流畅。必须在 `dispatchKeyEvent` 里拦——
  `PlayerView` 拿着焦点会先把方向键吃掉用于按钮间移动，`onKeyDown` 根本收不到
  （实测按 ⬇ 毫无反应）。
- **卡顿自动降档**。一分钟内卡 3 次就自动降一档。偶尔卡一下是网络抖动，
  降档反而降了画质；连着卡才说明这条码率扛不住。**只自动降一次**，
  之后交给用户手动选——反复自动切换比卡顿更烦人。

注意「卡顿」和「播放失败」是两回事：失败会抛 `PlaybackException`（走换源逻辑），
卡顿什么都不抛，只是 `STATE_BUFFERING` 来回跳，得自己数。

## 它不做什么

- **不做登录**。夸克/百度的扫码在网页端做，电视上扫码输码都难受。
- **不做批量转存**。找资源和单集转存有，「全部 N 版」那种长任务留在网页端。
- 不存任何凭据，只存服务器地址和访问令牌。

## 编译

```bash
cd androidtv
echo "sdk.dir=/你的/Android/Sdk" > local.properties   # Windows 用正斜杠
./gradlew :app:assembleDebug
```

产物在 `app/build/outputs/apk/debug/app-debug.apk`。release 用的是 debug 签名——
电视上装的是侧载包，没签名装不上，而这个 app 不上应用商店。

## 发布：让电视直接下载

电视上多半没有 adb，最省事的是让它自己去下。把 APK 放到服务器上，
用已有的 nginx 发出去（下面的域名/端口按自己的填）：

```nginx
# 放在反代 location / 之前，不进应用、不要令牌——
# 装 app 的场景就是「还没有令牌」，挡在令牌后面等于装不了。
# 包里不含任何凭据（服务器地址和令牌是用户自己在 app 里填的）。
location = /tv.apk {
    alias /var/www/mediafans/tv.apk;
    default_type application/vnd.android.package-archive;
    add_header Content-Disposition 'attachment; filename="MediaFans-TV.apk"';
    add_header Cache-Control "no-cache";
}
```

再在 80 端口加一条短地址，遥控器上少打几个字符。**只做 302，实际下载仍走 TLS**
——APK 用明文传有被掉包的风险，不能图省事直接在 80 上发文件：

```nginx
location = /tv.apk { return 302 https://$host:<https端口>/tv.apk; }
```

电视上用任意「下载器」类应用输入这个地址即可。装完记得核对 sha256，
跟本地 `app-debug.apk` 一致才对。

## 装到电视上（有 adb 的话）

电视和电脑在同一个网内：

```bash
adb connect <电视IP>:5555
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

电视上要先开「开发者选项 → USB 调试 / 网络调试」。装完在电视桌面能看到
MediaFans（`LEANBACK_LAUNCHER`）。

首次打开填两样：服务器地址（`https://主机:端口`）和访问令牌
（网页端 URL 里 `token=` 后面那串）。填一次就存下来了。

**手机上也能装**（调试方便）。设置页对小屏做过处理——早期版本在手机上
「填完地址找不到确认按钮」：主界面锁了横屏、页面又不能滚，输入法一弹，
「保存并进入」直接被顶到屏幕外。现在主界面不锁横屏、页面可滚动并给输入法让位，
而且**令牌那栏的键盘上直接有确认键**（✓）——小屏上那才是最先够得着的入口。
在 800×360dp（手机横屏，最挤的情况）下实测过。

## 结构

| 文件 | 干什么 |
|---|---|
| `Api.kt` | REST 客户端 + 数据模型。刻意用 HttpURLConnection + org.json，少两个依赖 |
| `Focus.kt` | `FocusBox`：焦点态同时给放大、描边、变色三个信号。电视上没有指针，一个信号不够 |
| `Screens.kt` | 设置 / 首页 / 搜索 / 作品页 |
| `PlayerActivity.kt` | ExoPlayer。默认原画、上下键切档、卡顿自动降档、每 15 秒和退出时上报进度 |

进度跟网页端共用服务端那份记录，所以**电视上看到一半，手机上接着看**。
