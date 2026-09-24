package com.mediafans.tv

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.MediaItem as ExoItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 播放页。用 View 不用 Compose——PlayerView 自带的控制条已经是电视语义
 * （左右键快进、确定键暂停），用 Compose 包一层反而要自己重写一遍。
 *
 * **这个页面是整个 TV 端存在的主要理由**：网页端受浏览器解码能力限制，
 * 原盘 MKV（HEVC 视频 + DTS-HD 音轨）既不报错也不播，只会黑屏一直下载；
 * 电视盒子有硬解，ExoPlayer 直接就能放——所以**原画在这里是可选的**，
 * 网页端连选都没得选。但默认仍是最高转码档：带宽比解码更硬（见 [PlayInfo.pick]）。
 *
 * 字幕同理：网页端只能放外挂的，这里外挂（服务端转好的 WebVTT）和
 * MKV 内封的都能放。
 */
@OptIn(UnstableApi::class)
class PlayerActivity : Activity() {

    private var player: ExoPlayer? = null
    private lateinit var view: PlayerView
    private lateinit var hint: TextView
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val ticker = Handler(Looper.getMainLooper())

    private lateinit var api: Api
    private lateinit var settings: Settings
    private lateinit var path: String
    /** 这个文件在哪个盘。从「继续观看」进来的可能跟当前设置不是同一个。 */
    private lateinit var nd: String
    private var displayName = ""
    private var startMs = 0L

    // 剧集上下文，用来把进度记成「某剧第几集」而不是一个孤立路径
    private var tmdbId = 0
    private var season = -1
    private var episode = -1
    private var mediaType = "tv"
    private var workTitle = ""
    private var workYear = ""
    private var workPoster = ""
    private var epTitle = ""

    private var streams: List<Stream> = emptyList()
    private var current: Stream? = null
    private var subtitles: List<Subtitle> = emptyList()
    private var announced = false

    /** 这次播放里已经失败过的档（key）。回退时跳过它们，否则两档都坏时会来回切。 */
    private val failedKeys = mutableSetOf<String>()
    /** 已经退到服务器中转过的档。每档只退一次，中转也挂了就换档。 */
    private val proxiedKeys = mutableSetOf<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        api = Api(settings)

        path = intent.getStringExtra(EX_PATH).orEmpty()
        nd = intent.getStringExtra(EX_ND)?.takeIf { it.isNotEmpty() } ?: settings.netdisk
        displayName = intent.getStringExtra(EX_NAME).orEmpty()
        startMs = intent.getLongExtra(EX_START, 0L)
        tmdbId = intent.getIntExtra(EX_TMDB, 0)
        season = intent.getIntExtra(EX_SEASON, -1)
        episode = intent.getIntExtra(EX_EPISODE, -1)
        mediaType = intent.getStringExtra(EX_MEDIA) ?: "tv"
        workTitle = intent.getStringExtra(EX_TITLE).orEmpty()
        workYear = intent.getStringExtra(EX_YEAR).orEmpty()
        workPoster = intent.getStringExtra(EX_POSTER).orEmpty()
        epTitle = intent.getStringExtra(EX_EPTITLE).orEmpty()

        val root = FrameLayout(this)
        view = PlayerView(this).apply {
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            setShowNextButton(false)
            setShowPreviousButton(false)
            // 有字幕轨（外挂或内封）时控制条上出现字幕按钮；没有就不显示
            setShowSubtitleButton(true)
            controllerShowTimeoutMs = 4000
        }
        hint = TextView(this).apply {
            setPadding(48, 48, 48, 48)
            textSize = 16f
            setTextColor(0xFFE8ECF4.toInt())
            text = "正在获取播放地址…"
        }
        root.addView(view)
        root.addView(hint)
        setContentView(root)

        loadAndPlay()
    }

    private fun loadAndPlay() {
        scope.launch {
            val info = runCatching { withContext(Dispatchers.IO) { api.play(path, nd) } }
                .getOrElse {
                    showHint("播放失败：${it.message}")
                    return@launch
                }
            streams = info.streams
            if (streams.isEmpty()) {
                showHint("这个文件没有可播放的地址")
                return@launch
            }
            // 服务端记的进度优先于调用方传进来的（跨设备接着看靠的就是它）
            if (startMs <= 0L) {
                runCatching { withContext(Dispatchers.IO) { api.watchGet(path, nd) } }
                    .onSuccess { if (it > 0) startMs = it * 1000L }
            }
            // 外挂字幕拿不到就当没有，不挡播放
            subtitles = runCatching { withContext(Dispatchers.IO) { api.subs(path, nd) } }
                .getOrDefault(emptyList())
            start(info.pick(settings.quality) ?: streams.first())
        }
    }

    private fun start(s: Stream) {
        current = s
        player?.release()
        hasPlayed = false
        rebuffers = 0

        // 直链是网盘 CDN 的绝对地址，代理是本机中转。UA 跟网页端保持一致，
        // 有些 CDN 会按 UA 拒绝。
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent("Mozilla/5.0 (Linux; Android 10) MediaFans/1.0")
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(20000)
            .setReadTimeoutMs(60000)

        val p = ExoPlayer.Builder(this)
            .setLoadControl(loadControl())
            .setRenderersFactory(
                // 软解兜底：电视盒子硬解不了某条轨时（少见但有），
                // 让它退到软解而不是直接黑屏
                DefaultRenderersFactory(this)
                    .setExtensionRendererMode(
                        DefaultRenderersFactory.EXTENSION_RENDERER_MODE_ON)
            )
            .setMediaSourceFactory(DefaultMediaSourceFactory(http))
            .build()

        val item = ExoItem.Builder()
            .setUri(s.url)
            .apply { if (s.mime.isNotEmpty()) setMimeType(guessMime(s.mime)) }
            .setSubtitleConfigurations(subtitleConfigs())
            .build()

        p.trackSelectionParameters = subtitleParams(p)
        p.setMediaItem(item)
        if (startMs > 0) p.seekTo(startMs)
        p.playWhenReady = true
        p.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                onFailed(s, error)
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_BUFFERING) onRebuffer(s)
                // 播完了记一笔：定时上报只在播放中发，最后那几秒会漏，
                // 结果就是「看完了」的集在列表里还显示 98%
                if (state == Player.STATE_ENDED) saveProgress()
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) hasPlayed = true
            }

            /**
             * 有音轨但一条都放不了——原盘 MKV 的 DTS-HD / TrueHD，盒子没解码器、
             * 也没接支持直通的功放。ExoPlayer 对此**不报错**：画面照放，只是没声音，
             * 上面 onPlayerError 那套回退根本不会触发。只能自己看轨道。
             */
            override fun onTracksChanged(tracks: Tracks) {
                val audio = tracks.groups.filter { it.type == C.TRACK_TYPE_AUDIO }
                if (audio.isNotEmpty() && audio.none { it.isSupported } && current == s) {
                    // 抛到下一轮再换：回调还在这个 player 的分发里，当场 release 不稳妥
                    ticker.post {
                        if (current == s) {
                            fallbackToTranscode(s, "放不了这个文件的音轨（多半是 DTS / TrueHD）")
                        }
                    }
                }
            }

            override fun onPositionDiscontinuity(
                oldPosition: Player.PositionInfo,
                newPosition: Player.PositionInfo,
                reason: Int,
            ) {
                if (reason == Player.DISCONTINUITY_REASON_SEEK) {
                    lastSeekAt = System.currentTimeMillis()
                }
            }
        })
        p.prepare()
        player = p
        view.player = p
        // 操作提示只在第一次起播时给。之后的 start() 都是换档/回退触发的，
        // 调用方已经显示了「换到 xx…」，这里再覆盖掉用户就不知道刚才为什么切了
        if (!announced) {
            announced = true
            if (streams.size > 1) {
                showHint(s.label + "　·　⬆更清晰 / ⬇更流畅　·　菜单键切字幕", 5000)
            } else {
                hint.visibility = View.GONE
            }
        }
        view.keepScreenOn = true
        view.requestFocus()
        startReporting()
    }

    // ------------------------------------------------------------ 字幕

    /**
     * 外挂字幕挂到 MediaItem 上。服务端已经把 srt/ass 统一转成了 WebVTT。
     *
     * 挑一条标 DEFAULT：中文优先，没有就第一条。ExoPlayer 只会自动选中
     * 「语言匹配偏好」或「带 DEFAULT 标记」的字幕轨——认不出语言的外挂字幕
     * （`字幕.srt` 这种）不标的话永远是关着的，用户得自己去找。
     */
    private fun subtitleConfigs(): List<ExoItem.SubtitleConfiguration> {
        if (subtitles.isEmpty()) return emptyList()
        val preferred = subtitles.indexOfFirst { it.lang.startsWith("zh") }
            .takeIf { it >= 0 } ?: 0
        val off = settings.subtitle == SUB_OFF
        return subtitles.mapIndexed { i, sub ->
            ExoItem.SubtitleConfiguration.Builder(Uri.parse(sub.url))
                .setMimeType(MimeTypes.TEXT_VTT)
                .setLabel(sub.label)
                .apply { if (sub.lang.isNotEmpty()) setLanguage(sub.lang) }
                .setSelectionFlags(if (i == preferred && !off) C.SELECTION_FLAG_DEFAULT else 0)
                .build()
        }
    }

    /**
     * 字幕轨怎么选。偏好空着就是「中文优先」——这同时管到 MKV 内封的字幕轨，
     * 它们的语言码通常是 chi/zho，ExoPlayer 会归一到 zh。
     */
    private fun subtitleParams(p: Player) = p.trackSelectionParameters.buildUpon()
        .setTrackTypeDisabled(C.TRACK_TYPE_TEXT, settings.subtitle == SUB_OFF)
        .setPreferredTextLanguages(
            *(settings.subtitle.takeIf { it.isNotEmpty() && it != SUB_OFF }
                ?.let { arrayOf(it, "zh") } ?: arrayOf("zh")))
        .build()

    /**
     * 菜单键 / 字幕键：关 → 第 1 条 → 第 2 条 → … → 关，循环。
     *
     * 控制条上也有字幕按钮，但遥控器要先调出控制条、再往右找好几格，
     * 看片时想开关一下字幕太远了。选了什么记下来跨文件沿用（记语言，不记哪条）。
     */
    private fun cycleSubtitle() {
        val p = player ?: return
        val groups = p.currentTracks.groups.filter {
            it.type == C.TRACK_TYPE_TEXT && it.isSupported
        }
        if (groups.isEmpty()) {
            showHint("这个文件没有字幕", 3000)
            return
        }
        val disabled = p.trackSelectionParameters.disabledTrackTypes
            .contains(C.TRACK_TYPE_TEXT)
        val cur = if (disabled) -1 else groups.indexOfFirst { it.isSelected }
        val next = cur + 1                       // -1 → 0，最后一条 → 关
        val b = p.trackSelectionParameters.buildUpon()
        if (next >= groups.size) {
            b.setTrackTypeDisabled(C.TRACK_TYPE_TEXT, true)
            settings.subtitle = SUB_OFF
            showHint("字幕：关", 3000)
        } else {
            val g = groups[next]
            b.setTrackTypeDisabled(C.TRACK_TYPE_TEXT, false)
                .setOverrideForType(TrackSelectionOverride(g.mediaTrackGroup, 0))
            val f = g.getTrackFormat(0)
            settings.subtitle = f.language.orEmpty()
            showHint("字幕：" + (f.label ?: f.language ?: "第${next + 1}条") +
                "（${next + 1}/${groups.size}）", 3000)
        }
        p.trackSelectionParameters = b.build()
    }

    // ------------------------------------------------------------ 提示

    private val hideHint = Runnable { hint.visibility = View.GONE }

    /**
     * 显示一行提示。[autoHideMs] > 0 时到点自动收起，0 = 一直显示（报错用）。
     *
     * 收起用同一个 Runnable、每次先撤掉旧的：原来每次 postDelayed 一个新的匿名
     * lambda，上一条「切到 xx…」的定时器会把紧接着出来的报错也收掉。
     */
    private fun showHint(text: String, autoHideMs: Long = 0) {
        ticker.removeCallbacks(hideHint)
        hint.text = text
        hint.visibility = View.VISIBLE
        if (autoHideMs > 0) ticker.postDelayed(hideHint, autoHideMs)
    }

    /**
     * 缓冲策略。默认那套是按「网速稳定的手机看转码流」调的，对这里不合适：
     * 原盘码率实测 12 Mbps（1.82GB / 19.8 分钟），是同一部片 4K 转码档的两倍，
     * 而电视多半挂 Wi-Fi。默认起播只等 2.5 秒、卡顿后只等 5 秒就恢复播放，
     * 缓冲还没垫起来就又开始放，于是一路走走停停。
     *
     * 加大到：缓冲目标 60 秒 / 最多 256MB，卡顿后等 8 秒再续播。
     * 代价是起播慢一点（2.5→4 秒），换来的是别一直卡。
     */
    private fun loadControl(): LoadControl = DefaultLoadControl.Builder()
        .setBufferDurationsMs(60_000, 120_000, 4_000, 8_000)
        .setTargetBufferBytes(256 * 1024 * 1024)
        .setPrioritizeTimeOverSizeThresholds(true)
        .build()

    /**
     * 这一条播不了就换下一条。
     *
     * **换什么取决于为什么失败**：
     *
     * - **解码失败**（盒子解不了这条码流，比如 4K HDR10 的 HEVC Main10）：
     *   换同一个文件的另一条路没有意义，同样的码流照样解不了。必须换到网盘的
     *   **转码档**（h264+aac，什么盒子都能解）。
     * - **取流失败**（直链被 CDN 拒、链接过期）：这才是该退到服务器中转的场景。
     *
     * 一开始这两种混成了一条路径，结果解码失败时要先白试一次代理才换档，
     * 用户得多等十几秒。模拟器上 c2.goldfish.hevc.decoder 解不了 Main10 L5.0，
     * 正好把这条路径跑了出来。
     */
    private fun onFailed(failed: Stream, error: PlaybackException) {
        val code = error.errorCode
        val at = player?.currentPosition ?: 0L
        if (at > 0) startMs = at
        if (code in 3001..3004 || code in 4001..4005) {
            fallbackToTranscode(failed, "解不了" + failed.label + "（" + error.errorCodeName + "）")
            return
        }

        val proxy = failed.proxyUrl
        if (failed.url != proxy && proxy.isNotEmpty() && failed.key !in proxiedKeys) {
            proxiedKeys += failed.key
            showHint("直链不可用，改用服务器中转…", 5000)
            start(failed.copy(url = proxy, direct = false))
            return
        }
        failedKeys += failed.key
        val next = streams.firstOrNull { it.key !in failedKeys }
        if (next != null) {
            showHint("${failed.label} 播放失败，换到 ${next.label}…", 5000)
            start(next)
            return
        }
        showHint("播放失败：${error.errorCodeName}\n${error.message.orEmpty()}")
    }

    /**
     * 解码层面的失败：换到一个没失败过的转码档，从高往低。
     * 转码档是 h264+aac，解不了的概率最低。按高度挑而不是随便拿第一个：
     * 4K 转码档解不了（弱盒子常见）时该退到 2K，而不是直接掉到 540p。
     */
    private fun fallbackToTranscode(failed: Stream, why: String) {
        val at = player?.currentPosition ?: 0L
        if (at > 0) startMs = at
        failedKeys += failed.key
        val fallback = streams
            .filter { !it.origin && it.key !in failedKeys }
            .maxByOrNull { it.height }
        if (fallback != null) {
            showHint("这台设备$why，换到" + fallback.label + "…", 5000)
            start(fallback)
        } else {
            showHint("这台设备$why，也没有别的转码档可换。" +
                "到网页端作品页点「全部 N 版」多存几个版本再来换。")
        }
    }

    // 卡顿计数。起播和拖进度条后的那次缓冲都不算——那是正常的，不是卡。
    private var rebuffers = 0
    private var rebufferWindowStart = 0L
    private var autoDownshifted = false
    /** 这条流真的放起来过没有。没放起来之前的缓冲都是起播。 */
    private var hasPlayed = false
    private var lastSeekAt = 0L

    /**
     * 卡了就自动降一档。
     *
     * 「卡顿」和「播放失败」不一样：失败会抛 PlaybackException，卡顿什么都不抛，
     * 只是 STATE_BUFFERING 来回跳。用户在电视前看到的就是走走停停，
     * 而 TV 端播放中原本没有切档入口，只能干等。
     *
     * 判据是**一分钟内卡 3 次**：偶尔卡一下是网络抖动，降档反而降了画质；
     * 连着卡才说明这条码率这台设备/这个网络扛不住。只自动降一次，
     * 之后交给用户手动选——反复自动切换比卡顿更烦人。
     */
    private fun onRebuffer(s: Stream) {
        val now = System.currentTimeMillis()
        // 起播不算：原来用「播放位置 > 3 秒」判断，续播时一上来位置就是二十分钟，
        // 起播缓冲被当成了卡顿。改成看这条流有没有真正放起来过。
        if (!hasPlayed) return
        // 拖进度条 / 快进之后的缓冲也不算：那是用户自己跳到了没缓冲的地方
        if (now - lastSeekAt < 5_000L) return
        if (now - rebufferWindowStart > 60_000L) {
            rebufferWindowStart = now
            rebuffers = 0
        }
        rebuffers++
        if (rebuffers < 3 || autoDownshifted) return

        val lower = lowerThan(s) ?: return
        autoDownshifted = true
        showHint("网络跟不上这一档，已自动降到" + lower.label + "（按⬆可以再切回去）", 6000)
        startMs = player?.currentPosition ?: startMs
        start(lower)
    }

    /** 比当前这条更省带宽的一档。原画在这里排最高——它就是没压过的那份。 */
    private fun lowerThan(s: Stream): Stream? {
        val ordered = streams.sortedByDescending { if (it.origin) Int.MAX_VALUE else it.height }
        val i = ordered.indexOfFirst { it.key == s.key }
        return if (i >= 0 && i + 1 < ordered.size) ordered[i + 1] else null
    }

    private fun guessMime(m: String): String = when {
        m.contains("mpegurl", true) -> MimeTypes.APPLICATION_M3U8
        m.contains("matroska", true) -> MimeTypes.VIDEO_MATROSKA
        m.contains("mp4", true) -> MimeTypes.VIDEO_MP4
        else -> m
    }

    // ------------------------------------------------------------ 进度上报

    private val report = object : Runnable {
        override fun run() {
            if (player?.isPlaying == true) saveProgress()
            ticker.postDelayed(this, 15000)
        }
    }

    /**
     * 上报当前进度。在后台线程发，不挂在 Activity 的协程作用域上：
     * 退出时（onStop 之后紧跟 onDestroy）作用域一取消，这一笔就丢了。
     */
    private fun saveProgress() {
        val p = player ?: return
        if (p.duration <= 0) return
        val pos = p.currentPosition / 1000.0
        val dur = p.duration / 1000.0
        Thread {
            runCatching {
                api.watchSave(
                    path = path, position = pos, duration = dur,
                    tmdbId = tmdbId.takeIf { it > 0 },
                    season = season.takeIf { it >= 0 },
                    episode = episode.takeIf { it >= 0 },
                    mediaType = mediaType,
                    title = workTitle, year = workYear, poster = workPoster,
                    epTitle = epTitle, name = displayName, nd = nd,
                )
            }
        }.start()
    }

    private fun startReporting() {
        ticker.removeCallbacks(report)
        ticker.postDelayed(report, 15000)
    }

    /**
     * 上下键切清晰度。
     *
     * 必须在 dispatchKeyEvent 里拦，不能用 onKeyDown：PlayerView 拿着焦点，
     * 它的控制条会先把方向键吃掉用于按钮间移动，Activity 的 onKeyDown 根本收不到。
     * 实测就是这样——按 ⬇ 毫无反应。dispatchKeyEvent 在分发给视图树之前。
     *
     * 为什么这个入口是刚需：网页端有一排清晰度按钮，电视端原本什么都没有。
     * 而「卡了想换一档」恰恰是电视上最常见的诉求（原盘码率是转码档的两倍）。
     */
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        val code = event.keyCode
        // 字幕键 / 菜单键：切字幕，控制条出没出来都一样
        if (code == KeyEvent.KEYCODE_CAPTIONS || code == KeyEvent.KEYCODE_MENU) {
            if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) cycleSubtitle()
            return true
        }
        // 上下键只在**控制条没出来时**切档。控制条出来了就还给它：
        // 不然上下键永远被这里吃掉，焦点下不到字幕/音轨/设置那排按钮上
        val updown = code == KeyEvent.KEYCODE_DPAD_UP || code == KeyEvent.KEYCODE_DPAD_DOWN
        if (updown && !view.isControllerFullyVisible) {
            if (event.action == KeyEvent.ACTION_DOWN) {
                switchBy(if (code == KeyEvent.KEYCODE_DPAD_UP) -1 else 1)
            }
            return true          // 抬起也吃掉，别再往下传
        }
        if (code == KeyEvent.KEYCODE_MEDIA_STOP && event.action == KeyEvent.ACTION_DOWN) {
            finish()
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    /** step=-1 往高画质走，+1 往低画质走。 */
    private fun switchBy(step: Int) {
        val cur = current ?: return
        if (streams.size < 2) return
        val ordered = streams.sortedByDescending { if (it.origin) Int.MAX_VALUE else it.height }
        val i = ordered.indexOfFirst { it.key == cur.key }
        val target = ordered.getOrNull(i + step) ?: return
        // 手动选过就别再自动降档了，用户比启发式清楚自己要什么。
        // 也记下来跨文件沿用——每集都要重按一次太折磨人。
        // 注意只记手动的：自动降档是对当下网络的临时反应，不该覆盖用户的选择。
        autoDownshifted = true
        settings.quality = target.key
        showHint("切到" + target.label + "…（⬆更清晰 / ⬇更流畅）", 5000)
        startMs = player?.currentPosition ?: startMs
        start(target)
    }

    override fun onStop() {
        super.onStop()
        // 退出前把进度记上，不然看到一半按返回就白看了。
        // 还要暂停：按主页键时 Activity 只是 stop 不是 destroy，
        // 原来播放器接着放，回到桌面还一直有声音
        saveProgress()
        player?.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        ticker.removeCallbacks(report)
        scope.cancel()
        player?.release()
        player = null
    }

    companion object {
        private const val EX_PATH = "path"
        private const val EX_NAME = "name"
        private const val EX_START = "start"
        private const val EX_TMDB = "tmdb"
        private const val EX_SEASON = "season"
        private const val EX_EPISODE = "episode"
        private const val EX_MEDIA = "media"
        private const val EX_TITLE = "title"
        private const val EX_YEAR = "year"
        private const val EX_POSTER = "poster"
        private const val EX_EPTITLE = "eptitle"
        private const val EX_ND = "nd"
        private const val SUB_OFF = "off"

        fun intent(
            ctx: Context, work: Work, ep: Episode, copy: Copy, startMs: Long, nd: String,
        ): Intent = Intent(ctx, PlayerActivity::class.java).apply {
            putExtra(EX_PATH, copy.path)
            putExtra(EX_ND, nd)
            putExtra(EX_NAME, copy.name)
            putExtra(EX_START, startMs)
            putExtra(EX_TMDB, work.tmdbId)
            // 电影没有季/集：传 -1，上报时会整个略掉这两个字段
            putExtra(EX_SEASON, if (work.isMovie) -1 else work.season)
            putExtra(EX_EPISODE, if (work.isMovie) -1 else ep.episode)
            putExtra(EX_MEDIA, work.mediaType)
            putExtra(EX_TITLE, work.title)
            putExtra(EX_YEAR, work.year)
            putExtra(EX_POSTER, work.poster)
            putExtra(EX_EPTITLE, if (work.isMovie) "" else ep.title)
        }

        fun intentForPath(
            ctx: Context, path: String, name: String, startMs: Long, nd: String,
        ): Intent =
            Intent(ctx, PlayerActivity::class.java).apply {
                putExtra(EX_PATH, path)
                putExtra(EX_ND, nd)
                putExtra(EX_NAME, name)
                putExtra(EX_START, startMs)
                putExtra(EX_SEASON, -1)
                putExtra(EX_EPISODE, -1)
            }
    }
}
