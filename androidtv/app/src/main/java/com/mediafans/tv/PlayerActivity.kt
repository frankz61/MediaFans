package com.mediafans.tv

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem as ExoItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.DefaultRenderersFactory
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
 * （左右键快进、上下键切轨、确定键暂停），用 Compose 包一层反而要自己重写一遍。
 *
 * **这个页面是整个 TV 端存在的主要理由**：网页端受浏览器解码能力限制，
 * 原盘 MKV（HEVC 视频 + DTS-HD 音轨）既不报错也不播，只会黑屏一直下载；
 * 电视盒子有硬解，ExoPlayer 直接就能放。所以这里**默认播原画**，
 * 而不是像网页端那样优先挑网盘转码档。
 */
@OptIn(UnstableApi::class)
class PlayerActivity : Activity() {

    private var player: ExoPlayer? = null
    private lateinit var view: PlayerView
    private lateinit var hint: TextView
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val ticker = Handler(Looper.getMainLooper())

    private lateinit var api: Api
    private lateinit var path: String
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        api = Api(Settings(this))

        path = intent.getStringExtra(EX_PATH).orEmpty()
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
            val info = runCatching { withContext(Dispatchers.IO) { api.play(path) } }
                .getOrElse {
                    hint.text = "播放失败：${it.message}"
                    return@launch
                }
            streams = info.streams
            if (streams.isEmpty()) {
                hint.text = "这个文件没有可播放的地址"
                return@launch
            }
            // 服务端记的进度优先于调用方传进来的（跨设备接着看靠的就是它）
            if (startMs <= 0L) {
                runCatching { withContext(Dispatchers.IO) { api.watchGet(path) } }
                    .onSuccess { if (it > 0) startMs = it * 1000L }
            }
            start(info.pick(preferOrigin = true) ?: streams.first())
        }
    }

    private fun start(s: Stream) {
        current = s
        hint.visibility = View.GONE
        player?.release()

        // 直链是网盘 CDN 的绝对地址，代理是本机中转。UA 跟网页端保持一致，
        // 有些 CDN 会按 UA 拒绝。
        val http = DefaultHttpDataSource.Factory()
            .setUserAgent("Mozilla/5.0 (Linux; Android 10) MediaFans/1.0")
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(20000)
            .setReadTimeoutMs(60000)

        val p = ExoPlayer.Builder(this)
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
            .build()

        p.setMediaItem(item)
        if (startMs > 0) p.seekTo(startMs)
        p.playWhenReady = true
        p.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                onFailed(s, error)
            }
        })
        p.prepare()
        player = p
        view.player = p
        view.keepScreenOn = true
        view.requestFocus()
        startReporting()
    }

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
        if (code in 3001..3004 || code in 4001..4005) {
            // 挑一个转码档：h264+aac，解不了的概率最低
            val fallback = streams.firstOrNull { !it.origin && it.key != failed.key }
            hint.visibility = View.VISIBLE
            if (fallback != null) {
                hint.text = "这台设备解不了原画（" + error.errorCodeName +
                    "），换到" + fallback.label + "…"
                startMs = player?.currentPosition ?: startMs
                start(fallback)
            } else {
                hint.text = "这台设备解不了这个文件（" + error.errorCodeName +
                    "），而它没有转码档。到网页端作品页点「全部 N 版」多存几个版本再来换。"
            }
            return
        }

        val proxy = failed.proxyUrl
        if (failed.url != proxy && proxy.isNotEmpty()) {
            hint.visibility = View.VISIBLE
            hint.text = "直链不可用，改用服务器中转…"
            startMs = player?.currentPosition ?: startMs
            start(failed.copy(url = proxy, direct = false))
            return
        }
        val next = streams.firstOrNull { it.key != failed.key }
        if (next != null) {
            hint.visibility = View.VISIBLE
            hint.text = "${failed.label} 播放失败，换到 ${next.label}…"
            startMs = player?.currentPosition ?: startMs
            start(next)
            return
        }
        hint.visibility = View.VISIBLE
        hint.text = "播放失败：${error.errorCodeName}\n${error.message.orEmpty()}"
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
            val p = player
            if (p != null && p.duration > 0 && p.isPlaying) {
                val pos = p.currentPosition / 1000.0
                val dur = p.duration / 1000.0
                scope.launch {
                    runCatching {
                        withContext(Dispatchers.IO) {
                            api.watchSave(
                                path = path, position = pos, duration = dur,
                                tmdbId = tmdbId.takeIf { it > 0 },
                                season = season.takeIf { it >= 0 },
                                episode = episode.takeIf { it >= 0 },
                                mediaType = mediaType,
                                title = workTitle, year = workYear, poster = workPoster,
                                epTitle = epTitle, name = displayName,
                            )
                        }
                    }
                }
            }
            ticker.postDelayed(this, 15000)
        }
    }

    private fun startReporting() {
        ticker.removeCallbacks(report)
        ticker.postDelayed(report, 15000)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        // 电视遥控器上「媒体停止」和返回都该退出播放
        if (keyCode == KeyEvent.KEYCODE_MEDIA_STOP) { finish(); return true }
        return super.onKeyDown(keyCode, event)
    }

    override fun onStop() {
        super.onStop()
        // 退出前把进度记上，不然看到一半按返回就白看了
        val p = player
        if (p != null && p.duration > 0) {
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
                        epTitle = epTitle, name = displayName,
                    )
                }
            }.start()
        }
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

        fun intent(
            ctx: Context, work: Work, ep: Episode, copy: Copy, startMs: Long,
        ): Intent = Intent(ctx, PlayerActivity::class.java).apply {
            putExtra(EX_PATH, copy.path)
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

        fun intentForPath(ctx: Context, path: String, name: String, startMs: Long): Intent =
            Intent(ctx, PlayerActivity::class.java).apply {
                putExtra(EX_PATH, path)
                putExtra(EX_NAME, name)
                putExtra(EX_START, startMs)
                putExtra(EX_SEASON, -1)
                putExtra(EX_EPISODE, -1)
            }
    }
}
