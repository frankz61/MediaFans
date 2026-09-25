package com.mediafans.tv

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * 服务端地址和访问令牌。
 *
 * 令牌有两种来源：账号密码登录换来的会话令牌（按人分片库和进度），
 * 或者服务端启动参数 `--token` 那个共享令牌（视为管理员，老版本就用它）。
 * 电视上没法每次手打，所以存起来。
 * 存的是 SharedPreferences —— 电视是家里自己的设备，不值得为此上 EncryptedSharedPreferences
 * （那会把 minSdk 顶上去，老盒子反而装不了）。
 */
class Settings(ctx: Context) {
    private val sp = ctx.getSharedPreferences("mediafans", Context.MODE_PRIVATE)

    var base: String
        get() = sp.getString("base", "")!!.trimEnd('/')
        set(v) = sp.edit().putString("base", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "")!!
        set(v) = sp.edit().putString("token", v.trim()).apply()

    /** 登录的是谁。`_master` = 共享令牌（管理员，没有自己的片库）；空 = 还没验过。 */
    var user: String
        get() = sp.getString("user", "")!!
        set(v) = sp.edit().putString("user", v).apply()

    /** 是不是一个真账号。共享令牌对应的内建管理员不在账号表里，片库对它不生效。 */
    val hasAccount: Boolean get() = user.isNotEmpty() && user != MASTER

    /** 网盘：quark / baidu。电视端只切换，不做登录——扫码在网页端做更方便。 */
    var netdisk: String
        get() = sp.getString("nd", "quark")!!
        set(v) = sp.edit().putString("nd", v).apply()

    /** 上次手动选过的清晰度（4k/super/high/low/origin），跨文件沿用。 */
    var quality: String
        get() = sp.getString("quality", "")!!
        set(v) = sp.edit().putString("quality", v).apply()

    /**
     * 字幕偏好，跨文件沿用：空 = 自动（有中文就开）；`off` = 关；
     * 其余是语言码（用户用字幕键手动选过的那条的语言）。
     */
    var subtitle: String
        get() = sp.getString("subtitle", "")!!
        set(v) = sp.edit().putString("subtitle", v).apply()

    val configured: Boolean get() = base.isNotEmpty()

    companion object {
        const val MASTER = "_master"
    }
}

/**
 * 把用户填的地址拆成「服务器地址 + 令牌」。
 *
 * 最常见的填法是直接把网页端地址栏整个抄过来，里面带着 `?token=...`，
 * 甚至还带着防扫描入口路径（`https://host/<entry>`）。令牌抠出来单独存；
 * 路径先留着，连不上时 [Api.resolveBase] 会退到根路径再试——
 * 接口永远挂在根上（`/api/...`），入口路径只管网页。
 */
fun splitAddress(raw: String): Pair<String, String> {
    var s = raw.trim()
    if (s.isEmpty()) return "" to ""
    if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
    val u = Uri.parse(s)
    val token = runCatching { u.getQueryParameter("token") }.getOrNull().orEmpty()
    val base = u.buildUpon().clearQuery().fragment(null).build().toString()
        .trimEnd('/')
    return base to token
}

// ---------------------------------------------------------------- 数据模型

data class MediaItem(
    val tmdbId: Int,
    val title: String,
    val year: String,
    val poster: String,
    val mediaType: String,          // tv | movie
    val rating: Double,
    val overview: String,
    /** 榜单里豆瓣按季上榜的剧（「花儿与少年 第八季」）：点进去直接落在这一季 */
    val season: Int? = null,
)

data class Copy(
    val path: String,
    val name: String,
    val sizeH: String,
    val height: Int,
    /** 网盘认它是视频吗。false = 没有转码档，多半是伪装类型上传的（见 README） */
    val transcodable: Boolean,
)

data class Source(
    val index: Int,
    val label: String,
    val name: String,
    val sizeH: String,
    val height: Int,
    val shareTitle: String,
)

data class Watched(val percent: Int, val finished: Boolean, val resumeAt: Int, val path: String)

/** 外挂字幕。服务端已经转成 WebVTT 了（srt/ass 都是），这边只管挂上去。 */
data class Subtitle(val label: String, val lang: String, val url: String)

/** 片库里的一条：只记「在追哪部」，进度是服务端顺带贴上的。 */
data class LibItem(
    val tmdbId: Int,
    val mediaType: String,
    val title: String,
    val year: String,
    val poster: String,
    val season: Int?,
    val watchedSeason: Int?,
    val watchedEpisode: Int?,
    val percent: Int,
    val finished: Boolean,
)

data class Me(val name: String, val admin: Boolean)

data class Episode(
    val episode: Int,
    val title: String,
    val airDate: String,
    val status: String,             // saved | available | missing
    val copies: List<Copy>,
    val sources: List<Source>,
    val watched: Watched?,
) {
    val local: Copy? get() = copies.firstOrNull()
}

data class SeasonInfo(val season: Int, val episodes: Int, val name: String)

data class Work(
    val tmdbId: Int,
    val title: String,
    val season: Int,
    val mediaType: String,
    val year: String,
    val overview: String,
    val poster: String,
    val runtime: Int,
    val localDir: String,
    val seasons: List<SeasonInfo>,
    val episodes: List<Episode>,
    val notes: List<String>,
) {
    val isMovie: Boolean get() = mediaType == "movie"
}

data class Stream(
    val key: String,
    val label: String,
    val url: String,
    val proxyUrl: String,
    val direct: Boolean,
    val height: Int,
    val sizeH: String,
    val origin: Boolean,
    val mime: String,
)

// 夸克的清晰度阶梯：档位 key -> 标称高度。origin 没有固定高度，单独当最高档处理。
private val LADDER = mapOf("4k" to 2160, "2k" to 1440, "super" to 810,
                           "high" to 540, "low" to 270, "normal" to 540)

data class PlayInfo(
    val fileName: String,
    val streams: List<Stream>,
    val defaultKey: String,
    val direct: Boolean,
) {
    /**
     * 默认播**最高的转码档**，不是原画。
     *
     * 一开始这里是原画优先，理由是电视盒子能硬解 HEVC、而转码档是网盘二次压缩过的。
     * 解码能力这条没错，但**带宽这条更硬**：实测同一集原盘 1.82GB / 19.8 分钟
     * 约 12.2 Mbps，而它的 4K 转码档只要 6380 kbps，正好一半。电视挂 Wi-Fi，
     * 原画实际播不动——画质再好，卡着就是不能看。
     *
     * 原画仍然一按 ⬆ 就能切回去，网络好的时候值得。
     *
     * 手动选过的档优先（`want`）：用户比这里的启发式清楚自己家的网。
     * 那一档这个文件没有时，退到高度最接近且不超过它的一档——跟网页端同一套规则。
     */
    fun pick(want: String = ""): Stream? {
        if (streams.isEmpty()) return null
        if (want.isNotEmpty()) {
            streams.firstOrNull { it.key == want }?.let { return it }
            // 这个文件没有那一档：退到高度最接近且不超过它的一档。
            // 高度得查固定阶梯——从 streams 里查是查不到的，能查到就已经 return 了。
            LADDER[want]?.let { wantH ->
                streams.filter { !it.origin && it.height in 1..wantH }
                    .maxByOrNull { it.height }?.let { return it }
            }
        }
        // 转码档是 h264+aac，码率也低一半，是「能播且够看」的那个选择
        streams.filter { !it.origin && it.height > 0 }.maxByOrNull { it.height }
            ?.let { return it }
        return streams.firstOrNull { !it.origin }
            ?: streams.firstOrNull { it.key == defaultKey }
            ?: streams.first()
    }
}

data class RecentItem(
    val path: String,
    val playPath: String,
    val netdisk: String,
    val title: String,
    val name: String,
    val poster: String,
    val year: String,
    val tmdbId: Int,
    val season: Int?,
    val episode: Int?,
    val mediaType: String,
    val percent: Int,
    val position: Double,
    val duration: Double,
    val finished: Boolean,
)

// ---------------------------------------------------------------- HTTP

/** [needLogin]：服务端回了 401——令牌过期、被改密码作废、或者压根不对。该回登录页了。 */
class ApiError(message: String, val needLogin: Boolean = false) : Exception(message)

/**
 * 服务端 REST 客户端。
 *
 * 刻意不引 Retrofit/OkHttp：这套接口一共十来个、返回都是扁平 JSON，
 * HttpURLConnection + org.json 就够，还能少两个依赖（电视盒子上 APK 越小越好装）。
 */
class Api(private val settings: Settings) {

    private fun url(
        path: String,
        params: Map<String, String> = emptyMap(),
        base: String = settings.base,
        auth: Boolean = true,
    ): String {
        val sb = StringBuilder(base).append(path)
        val all = LinkedHashMap(params)
        if (auth && settings.token.isNotEmpty()) all["token"] = settings.token
        if (all.isNotEmpty()) {
            sb.append('?')
            sb.append(all.entries.joinToString("&") {
                "${it.key}=${URLEncoder.encode(it.value, "UTF-8")}"
            })
        }
        return sb.toString()
    }

    private fun request(method: String, full: String, body: String? = null): JSONObject {
        if (full.startsWith("/")) throw ApiError("还没设置服务器地址")
        val c = (URL(full).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15000
            readTimeout = 60000
            setRequestProperty("Accept", "application/json")
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
            }
        }
        try {
            if (body != null) c.outputStream.use { it.write(body.toByteArray()) }
            val code = c.responseCode
            val text = (if (code in 200..299) c.inputStream else c.errorStream)
                ?.bufferedReader()?.use(BufferedReader::readText) ?: ""
            if (text.isEmpty()) throw ApiError("HTTP $code：服务端没有返回内容")
            val obj = try {
                JSONObject(text)
            } catch (e: Exception) {
                // 返回的是 HTML：多半是地址填错了——服务端对不认识的路径一律回
                // 一个标准 404 页（防扫描），或者前面的 nginx 回了自己的错误页
                throw ApiError(when (code) {
                    404 -> "这个地址上没有 MediaFans 服务（HTTP 404），检查服务器地址"
                    401, 403 -> "访问令牌不对"
                    else -> "HTTP $code：返回的不是 JSON"
                }, needLogin = code == 401 || code == 403)
            }
            if (code == 401) {
                throw ApiError(obj.optString("error").ifEmpty { "需要登录" }, needLogin = true)
            }
            obj.optString("error").takeIf { it.isNotEmpty() }?.let { throw ApiError(it) }
            return obj
        } finally {
            c.disconnect()
        }
    }

    private fun get(path: String, params: Map<String, String> = emptyMap()) =
        request("GET", url(path, params))

    private fun post(path: String, body: JSONObject) =
        request("POST", url(path), body.toString())

    /** 直链/代理地址都可能是相对路径，播放前补成绝对地址（代理还要带令牌）。 */
    fun absolute(u: String): String {
        if (u.startsWith("http://") || u.startsWith("https://")) return u
        val sep = if (u.contains('?')) "&" else "?"
        val tok = if (settings.token.isEmpty()) "" else "${sep}token=${settings.token}"
        return settings.base + u + tok
    }

    // ------------------------------------------------------------ 账号

    /**
     * 找到接口真正挂在哪。先试用户填的原样，不行再退到根路径：
     * 从网页端地址栏抄来的地址常带着入口路径，而接口永远在根上。
     *
     * 判据是 `/api/me` 回的是不是 JSON——没登录时它回 401 + JSON，
     * 这就足以证明「这里是 MediaFans」，不需要先有令牌。
     */
    fun resolveBase(raw: String): String {
        val u = Uri.parse(raw)
        val root = "${u.scheme}://${u.encodedAuthority}"
        val candidates = listOf(raw, root).distinct()
        var last: Exception? = null
        for (b in candidates) {
            try {
                request("GET", url("/api/me", base = b, auth = false))
                return b
            } catch (e: ApiError) {
                if (e.needLogin) return b      // 401 JSON：地址对了，只差登录
                last = e
            } catch (e: Exception) {
                last = e
            }
        }
        throw ApiError("连不上服务器：${last?.message ?: raw}")
    }

    /** 账号密码换会话令牌。这一步本身不带令牌。 */
    fun login(base: String, username: String, password: String): String =
        request("POST", url("/api/login", base = base, auth = false),
            JSONObject().put("username", username).put("password", password).toString())
            .getString("token")

    /** 当前令牌是谁。连带验证令牌还有没有效。 */
    fun me(): Me {
        val u = get("/api/me").getJSONObject("user")
        return Me(u.optString("name"), u.optBoolean("admin"))
    }

    /** 服务端作废这个会话。失败也无所谓，本地照样清掉。 */
    fun logout() {
        runCatching { post("/api/logout", JSONObject()) }
    }

    // ------------------------------------------------------------ 片库

    fun library(): List<LibItem> {
        val a = get("/api/library").optJSONArray("items") ?: return emptyList()
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            val w = o.optJSONObject("watched")
            LibItem(
                tmdbId = o.optInt("tmdb_id"),
                mediaType = o.optString("media_type", "tv"),
                title = o.optString("title"),
                year = o.optString("year"),
                poster = o.optString("poster"),
                season = if (o.isNull("season")) null else o.optInt("season"),
                watchedSeason = w?.let { if (it.isNull("season")) null else it.optInt("season") },
                watchedEpisode = w?.let { if (it.isNull("episode")) null else it.optInt("episode") },
                percent = w?.optInt("percent") ?: 0,
                finished = w?.optBoolean("finished") ?: false,
            )
        }
    }

    fun libraryAdd(w: Work) {
        post("/api/library/add", JSONObject().apply {
            put("tmdb_id", w.tmdbId); put("media_type", w.mediaType)
            put("title", w.title); put("year", w.year); put("poster", w.poster)
            if (!w.isMovie) put("season", w.season)
        })
    }

    fun libraryRemove(tmdbId: Int, mediaType: String) {
        post("/api/library/remove", JSONObject().apply {
            put("tmdb_id", tmdbId); put("media_type", mediaType)
        })
    }

    // ------------------------------------------------------------ 榜单 / 搜索

    fun discover(kind: String): List<MediaItem> =
        get("/api/discover", mapOf("kind" to kind)).getJSONArray("items").toMediaList()

    fun searchMedia(kw: String): List<MediaItem> =
        get("/api/search/media", mapOf("kw" to kw)).getJSONArray("items").toMediaList()

    private fun JSONArray.toMediaList(): List<MediaItem> = (0 until length()).map { i ->
        val o = getJSONObject(i)
        MediaItem(
            tmdbId = o.optInt("tmdb_id"),
            title = o.optString("title"),
            year = o.optString("year"),
            poster = o.optString("poster"),
            mediaType = o.optString("media_type", "tv"),
            rating = o.optDouble("rating", 0.0),
            overview = o.optString("overview"),
            season = o.optInt("season").takeIf { it > 0 },
        )
    }

    // ------------------------------------------------------------ 作品

    fun work(
        tmdbId: Int, season: Int, media: String, refresh: Boolean = false,
        nd: String = settings.netdisk,
    ): Work {
        val p = mutableMapOf(
            "tmdb_id" to tmdbId.toString(),
            "season" to season.toString(),
            "media" to media,
            "nd" to nd,
        )
        if (refresh) p["refresh"] = "1"
        return get("/api/series", p).toWork()
    }

    private fun JSONObject.toWork(): Work {
        val seasons = optJSONArray("seasons")?.let { a ->
            (0 until a.length()).map {
                val s = a.getJSONObject(it)
                SeasonInfo(s.optInt("season"), s.optInt("episodes"), s.optString("name"))
            }
        } ?: emptyList()
        val eps = optJSONArray("episodes")?.let { a ->
            (0 until a.length()).map { i ->
                val e = a.getJSONObject(i)
                Episode(
                    episode = e.optInt("episode"),
                    title = e.optString("title"),
                    airDate = e.optString("air_date"),
                    status = e.optString("status"),
                    copies = e.optJSONArray("copies")?.let { c ->
                        (0 until c.length()).map { j ->
                            val x = c.getJSONObject(j)
                            Copy(
                                path = x.optString("path"),
                                name = x.optString("name"),
                                sizeH = x.optString("size_h"),
                                height = x.optInt("height"),
                                transcodable = x.optBoolean("transcodable", true),
                            )
                        }
                    } ?: emptyList(),
                    sources = e.optJSONArray("sources")?.let { s ->
                        (0 until s.length()).map { j ->
                            val x = s.getJSONObject(j)
                            Source(
                                index = x.optInt("index"),
                                label = x.optString("label"),
                                name = x.optString("name"),
                                sizeH = x.optString("size_h"),
                                height = x.optInt("height"),
                                shareTitle = x.optString("share_title"),
                            )
                        }
                    } ?: emptyList(),
                    watched = e.optJSONObject("watched")?.let {
                        Watched(
                            it.optInt("percent"), it.optBoolean("finished"),
                            it.optInt("resume_at"), it.optString("path"),
                        )
                    },
                )
            }
        } ?: emptyList()
        return Work(
            tmdbId = optInt("tmdb_id"),
            title = optString("title"),
            season = optInt("season"),
            mediaType = optString("media_type", "tv"),
            year = optString("year"),
            overview = optString("overview"),
            poster = optString("poster"),
            runtime = optInt("runtime"),
            localDir = optString("local_dir"),
            seasons = seasons,
            episodes = eps,
            notes = optJSONArray("notes")?.let { a ->
                (0 until a.length()).map { a.getString(it) }
            } ?: emptyList(),
        )
    }

    // ------------------------------------------------------------ 播放

    fun play(path: String, nd: String = settings.netdisk): PlayInfo {
        val o = get("/api/play", mapOf("path" to path, "nd" to nd))
        val arr = o.optJSONArray("streams")
        val streams = (0 until (arr?.length() ?: 0)).map { i ->
            val s = arr!!.getJSONObject(i)
            Stream(
                key = s.optString("key"),
                label = s.optString("label"),
                url = absolute(s.optString("url")),
                proxyUrl = absolute(s.optString("proxy_url")),
                direct = s.optBoolean("direct"),
                height = s.optInt("height"),
                sizeH = s.optString("size_h"),
                origin = s.optBoolean("origin"),
                mime = s.optString("mime"),
            )
        }
        return PlayInfo(
            fileName = o.optString("file_name"),
            streams = streams,
            defaultKey = o.optString("default_key"),
            direct = o.optBoolean("direct"),
        )
    }

    /**
     * 同目录的外挂字幕。拿不到就当没有——字幕是锦上添花，不该挡住播放。
     * 地址是 `/subs?t=...`，跟 `/stream` 一样要带令牌，所以也过一遍 [absolute]。
     */
    fun subs(path: String, nd: String = settings.netdisk): List<Subtitle> {
        val a = get("/api/subs", mapOf("path" to path, "nd" to nd))
            .optJSONArray("items") ?: return emptyList()
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            Subtitle(o.optString("label"), o.optString("lang"), absolute(o.optString("url")))
        }
    }

    // ------------------------------------------------------------ 进度

    fun recent(limit: Int = 20): List<RecentItem> {
        val a = get("/api/watch/recent", mapOf("limit" to limit.toString()))
            .getJSONArray("items")
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            RecentItem(
                path = o.optString("path"),
                playPath = o.optString("play_path"),
                netdisk = o.optString("netdisk", "quark"),
                title = o.optString("title"),
                name = o.optString("name"),
                poster = o.optString("poster"),
                year = o.optString("year"),
                tmdbId = o.optInt("tmdb_id"),
                season = if (o.isNull("season")) null else o.optInt("season"),
                episode = if (o.isNull("episode")) null else o.optInt("episode"),
                mediaType = o.optString("media_type", "tv"),
                percent = o.optInt("percent"),
                position = o.optDouble("position", 0.0),
                duration = o.optDouble("duration", 0.0),
                finished = o.optBoolean("finished"),
            )
        }
    }

    fun watchGet(path: String, nd: String = settings.netdisk): Int =
        get("/api/watch", mapOf("path" to path, "nd" to nd))
            .optJSONObject("mark")?.optInt("resume_at") ?: 0

    /** 上报进度。带上剧集上下文，「最近观看」才能按作品聚合而不是一堆孤立文件。 */
    fun watchSave(
        path: String, position: Double, duration: Double,
        tmdbId: Int?, season: Int?, episode: Int?, mediaType: String,
        title: String, year: String, poster: String, epTitle: String, name: String,
        nd: String = settings.netdisk,
    ) {
        val body = JSONObject().apply {
            put("path", path)
            put("position", position)
            put("duration", duration)
            put("netdisk", nd)
            put("media_type", mediaType)
            put("title", title)
            put("year", year)
            put("poster", poster)
            put("ep_title", epTitle)
            put("name", name)
            if (tmdbId != null && tmdbId > 0) put("tmdb_id", tmdbId)
            if (season != null) put("season", season)
            if (episode != null) put("episode", episode)
        }
        post("/api/watch", body)
    }

    // ------------------------------------------------------------ 转存

    /** 转存单个来源。同步返回，服务端这一步本身很快。 */
    fun fetchEpisode(
        tmdbId: Int, season: Int, episode: Int, index: Int, nd: String = settings.netdisk,
    ): String =
        post("/api/episode/fetch", JSONObject().apply {
            put("tmdb_id", tmdbId); put("season", season)
            put("episode", episode); put("index", index)
            put("netdisk", nd)
        }).optString("path")

    /** 找资源 / 转存全部版本都是后台任务，返回 job id 后轮询。 */
    fun scan(tmdbId: Int, season: Int, media: String, nd: String = settings.netdisk): String =
        post("/api/series/scan", JSONObject().apply {
            put("tmdb_id", tmdbId); put("season", season)
            put("media", media); put("netdisk", nd)
        }).getString("job")

    fun fetchAll(tmdbId: Int, season: Int, media: String, episode: Int): String =
        post("/api/episode/fetch/all", JSONObject().apply {
            put("tmdb_id", tmdbId); put("season", season)
            put("media", media); put("episode", episode)
            put("netdisk", settings.netdisk)
        }).getString("job")

    data class Job(val done: Boolean, val error: String, val steps: List<String>)

    fun jobStatus(job: String): Job {
        val o = get("/api/auto/status", mapOf("job" to job))
        val a = o.optJSONArray("steps")
        return Job(
            done = o.optBoolean("done"),
            error = o.optString("error"),
            steps = (0 until (a?.length() ?: 0)).map {
                a!!.getJSONObject(it).optString("message")
            },
        )
    }

    /** 图片地址交给 Coil，TMDB 的海报是公网直连，不经服务端。 */
    fun posterUri(u: String): Uri? = if (u.isEmpty()) null else Uri.parse(u)
}
