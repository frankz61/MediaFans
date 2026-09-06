package com.mediafans.tv

import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

// 电视都有过扫描（画面边缘会被切掉），四周留白，别把内容顶到屏幕边上
private val OVERSCAN = PaddingValues(horizontal = 40.dp, vertical = 24.dp)

@Composable
private fun Title(text: String, size: Int = 26) =
    Text(text, color = T.Text, fontSize = size.sp, fontWeight = FontWeight.SemiBold)

@Composable
private fun Sub(text: String, size: Int = 14, color: Color = T.Dim) =
    Text(text, color = color, fontSize = size.sp, maxLines = 2,
        overflow = TextOverflow.Ellipsis)

/** 一个能聚焦的按钮。电视上按钮要够大，遥控器不是鼠标，点不准就是点不着。 */
@Composable
fun TvButton(
    text: String,
    modifier: Modifier = Modifier,
    focusRequester: FocusRequester? = null,
    onClick: () -> Unit,
) {
    FocusBox(
        onClick = onClick,
        modifier = if (focusRequester != null) modifier.focusRequester(focusRequester)
                   else modifier,
        shape = RoundedCornerShape(8.dp),
        focusedScale = 1.05f,
    ) { focused ->
        Box(
            Modifier
                .background(if (focused) T.Accent else T.Panel)
                .padding(horizontal = 22.dp, vertical = 12.dp),
        ) {
            Text(text, color = if (focused) Color.White else T.Text, fontSize = 16.sp)
        }
    }
}

// ---------------------------------------------------------------- 设置

@Composable
fun SetupScreen(settings: Settings, onDone: () -> Unit) {
    var base by remember { mutableStateOf(settings.base) }
    var token by remember { mutableStateOf(settings.token) }
    var nd by remember { mutableStateOf(settings.netdisk) }
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }

    Column(Modifier.fillMaxSize().padding(OVERSCAN), verticalArrangement = Arrangement.Center) {
        Title("连接到 MediaFans 服务", 30)
        Spacer(Modifier.height(6.dp))
        Sub("填网页端那台服务的地址和令牌。电视上打字麻烦，填一次就够了。")
        Spacer(Modifier.height(24.dp))

        Field("服务器地址", base, "https://example.com:12583", first) { base = it }
        Spacer(Modifier.height(14.dp))
        Field("访问令牌", token, "网页端 URL 里 token= 后面那串") { token = it }
        Spacer(Modifier.height(20.dp))

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Sub("网盘", 16, T.Text)
            TvButton(if (nd == "quark") "● 夸克" else "○ 夸克") { nd = "quark" }
            TvButton(if (nd == "baidu") "● 百度" else "○ 百度") { nd = "baidu" }
        }
        Spacer(Modifier.height(24.dp))
        TvButton("保存并进入") {
            settings.base = base
            settings.token = token
            settings.netdisk = nd
            if (settings.configured) onDone()
        }
    }
}

@Composable
private fun Field(
    label: String,
    value: String,
    hint: String,
    focusRequester: FocusRequester? = null,
    onChange: (String) -> Unit,
) {
    Column {
        Sub(label, 14)
        Spacer(Modifier.height(4.dp))
        var focused by remember { mutableStateOf(false) }
        BasicTextField(
            value = value,
            onValueChange = onChange,
            singleLine = true,
            textStyle = TextStyle(color = T.Text, fontSize = 18.sp),
            cursorBrush = androidx.compose.ui.graphics.SolidColor(T.Accent),
            modifier = (focusRequester?.let { Modifier.focusRequester(it) } ?: Modifier)
                .width(700.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(T.Panel)
                .padding(14.dp)
                .onFocusChanged { focused = it.isFocused },
            decorationBox = { inner ->
                if (value.isEmpty()) Text(hint, color = T.Dim, fontSize = 18.sp)
                inner()
            },
        )
    }
}

// ---------------------------------------------------------------- 首页

private data class Rail(val title: String, val kind: String)

private val RAILS = listOf(
    Rail("剧集 · 今日播出", "airing"),
    Rail("剧集 · 一周在播", "onair"),
    Rail("电影 · 正在上映", "now"),
    Rail("剧集 · 热门", "popular"),
    Rail("电影 · 热门", "hot"),
)

@Composable
fun HomeScreen(
    api: Api,
    settings: Settings,
    onOpen: (MediaItem) -> Unit,
    onResume: (RecentItem) -> Unit,
    onSearch: () -> Unit,
    onSetup: () -> Unit,
) {
    var recent by remember { mutableStateOf<List<RecentItem>>(emptyList()) }
    val rails = remember { mutableStateMapOf<String, List<MediaItem>>() }
    var err by remember { mutableStateOf("") }
    val first = remember { FocusRequester() }

    LaunchedEffect(settings.netdisk) {
        err = ""
        // 每条榜单单独取、单独失败：TMDB 某一档挂了不该让整个首页空着
        runCatching { withContext(Dispatchers.IO) { api.recent(20) } }
            .onSuccess { recent = it }
            .onFailure { err = it.message ?: "读取最近观看失败" }
        for (r in RAILS) {
            runCatching { withContext(Dispatchers.IO) { api.discover(r.kind) } }
                .onSuccess { rails[r.kind] = it }
        }
        runCatching { first.requestFocus() }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = OVERSCAN,
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("MediaFans", color = T.Accent, fontSize = 30.sp,
                    fontWeight = FontWeight.Bold)
                Spacer(Modifier.width(20.dp))
                TvButton("搜索", focusRequester = first, onClick = onSearch)
                TvButton(if (settings.netdisk == "baidu") "百度网盘" else "夸克网盘",
                    onClick = onSetup)
                if (err.isNotEmpty()) Sub(err, 14, T.Err)
            }
        }
        if (recent.isNotEmpty()) {
            item {
                Column {
                    Title("继续观看", 22)
                    Spacer(Modifier.height(10.dp))
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        items(recent, key = { it.path }) { RecentCard(it, onResume) }
                    }
                }
            }
        }
        items(RAILS, key = { it.kind }) { rail ->
            val list = rails[rail.kind].orEmpty()
            if (list.isNotEmpty()) {
                Column {
                    Title(rail.title, 22)
                    Spacer(Modifier.height(10.dp))
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        items(list, key = { it.tmdbId }) { PosterCard(it, onOpen) }
                    }
                }
            }
        }
    }
}

@Composable
private fun PosterCard(item: MediaItem, onOpen: (MediaItem) -> Unit) {
    Column(Modifier.width(150.dp)) {
        FocusBox(onClick = { onOpen(item) }) { _ ->
            AsyncImage(
                model = item.poster,
                contentDescription = item.title,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxWidth().aspectRatio(2f / 3f).background(T.Panel),
            )
        }
        Spacer(Modifier.height(6.dp))
        Text(item.title, color = T.Text, fontSize = 14.sp, maxLines = 1,
            overflow = TextOverflow.Ellipsis)
        Sub(buildString {
            append(item.year)
            if (item.mediaType == "movie") append("　影")
            if (item.rating > 0) append("　★${item.rating}")
        }, 12)
    }
}

@Composable
private fun RecentCard(r: RecentItem, onResume: (RecentItem) -> Unit) {
    Column(Modifier.width(150.dp)) {
        FocusBox(onClick = { onResume(r) }) { _ ->
            Box(Modifier.fillMaxWidth().aspectRatio(2f / 3f).background(T.Panel)) {
                if (r.poster.isNotEmpty()) {
                    AsyncImage(r.poster, r.title, Modifier.fillMaxSize(),
                        contentScale = ContentScale.Crop)
                }
                // 进度条压在海报底部：一眼看出看到哪儿了
                Box(
                    Modifier.align(Alignment.BottomStart).fillMaxWidth()
                        .height(4.dp).background(Color(0x66000000)),
                ) {
                    Box(
                        Modifier
                            .fillMaxWidth(r.percent.coerceIn(0, 100) / 100f)
                            .height(4.dp)
                            .background(T.Accent),
                    )
                }
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(r.title.ifEmpty { r.name }, color = T.Text, fontSize = 14.sp, maxLines = 1,
            overflow = TextOverflow.Ellipsis)
        Sub(
            if (r.season != null && r.episode != null) "第${r.season}季 第${r.episode}集"
            else if (r.mediaType == "movie") "电影" else r.name,
            12,
        )
    }
}

// ---------------------------------------------------------------- 搜索

@Composable
fun SearchScreen(api: Api, onOpen: (MediaItem) -> Unit) {
    var kw by remember { mutableStateOf("") }
    var items by remember { mutableStateOf<List<MediaItem>>(emptyList()) }
    var state by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    val box = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { box.requestFocus() } }

    val search = {
        if (kw.isNotBlank()) {
            state = "搜索中…"
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { api.searchMedia(kw) } }
                    .onSuccess { items = it; state = if (it.isEmpty()) "没找到" else "" }
                    .onFailure { state = it.message ?: "搜索失败" }
            }
        }
    }

    Column(Modifier.fillMaxSize().padding(OVERSCAN)) {
        Title("搜索剧名 / 片名", 26)
        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Field("", kw, "如：末日地堡 / 沙丘", box) { kw = it }
            TvButton("搜索", onClick = search)
        }
        if (state.isNotEmpty()) {
            Spacer(Modifier.height(10.dp))
            Sub(state, 16)
        }
        Spacer(Modifier.height(16.dp))
        LazyRow(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            items(items, key = { it.tmdbId }) { PosterCard(it, onOpen) }
        }
    }
}

// ---------------------------------------------------------------- 作品页

@Composable
fun DetailScreen(
    api: Api,
    tmdbId: Int,
    media: String,
    titleHint: String,
    onPlay: (Work, Episode, Copy, Long) -> Unit,
) {
    var season by remember { mutableStateOf(if (media == "movie") 0 else 1) }
    var work by remember { mutableStateOf<Work?>(null) }
    var msg by remember { mutableStateOf("读取中…") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val first = remember { FocusRequester() }

    suspend fun load(refresh: Boolean = false) {
        runCatching { withContext(Dispatchers.IO) { api.work(tmdbId, season, media, refresh) } }
            .onSuccess { work = it; msg = "" }
            .onFailure { msg = it.message ?: "读取失败" }
    }
    LaunchedEffect(tmdbId, season) { load() }
    // 焦点必须等按钮真的组合出来再要。跟 load() 写在一个 effect 里不行：
    // `work = it` 之后紧接着 requestFocus，那时重组还没提交，按钮不存在，
    // requestFocus 静默失败——表现就是进页面后遥控器按什么都没反应。
    LaunchedEffect(work != null) {
        if (work != null) runCatching { first.requestFocus() }
    }

    val w = work
    Column(Modifier.fillMaxSize().padding(OVERSCAN)) {
        Title(if (w != null) "${w.title}${if (w.year.isNotEmpty()) "（${w.year}）" else ""}"
              else titleHint, 28)
        if (msg.isNotEmpty()) { Spacer(Modifier.height(8.dp)); Sub(msg, 16) }

        if (w != null) {
            Spacer(Modifier.height(10.dp))
            // 季切换。电影只有一行，这里改放年份和片长
            if (w.isMovie) {
                Sub(listOfNotNull(
                    w.year.takeIf { it.isNotEmpty() },
                    w.runtime.takeIf { it > 0 }?.let { "$it 分钟" },
                ).joinToString("　·　"), 15)
            } else if (w.seasons.size > 1) {
                LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    items(w.seasons, key = { it.season }) { s ->
                        TvButton(
                            (if (s.season == w.season) "● " else "") + "第${s.season}季",
                        ) { season = s.season }
                    }
                }
            }
            Spacer(Modifier.height(8.dp))

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                // 初始焦点挂在这里而不是第一集：LaunchedEffect 跑的时候
                // LazyColumn 还没排出第一项，requestFocus 会静默失败，
                // 结果就是进页面后遥控器按什么都没反应
                TvButton(if (w.isMovie) "找资源" else "找缺失的集",
                         focusRequester = first) {
                    if (!busy) {
                        busy = true
                        msg = "搜索中…"
                        scope.launch {
                            runCatching {
                                withContext(Dispatchers.IO) {
                                    val job = api.scan(tmdbId, season, media)
                                    pollJob(api, job) { msg = it }
                                }
                            }.onFailure { msg = it.message ?: "搜索失败" }
                            load(refresh = true)
                            busy = false
                        }
                    }
                }
            }
            Spacer(Modifier.height(14.dp))

            LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(w.episodes, key = { it.episode }) { ep ->
                    EpisodeRow(
                        work = w,
                        ep = ep,
                        onPlay = { copy ->
                            val at = ep.watched
                                ?.takeIf { !it.finished && it.path == copy.path }
                                ?.resumeAt ?: 0
                            onPlay(w, ep, copy, at.toLong() * 1000)
                        },
                        onFetch = { src ->
                            if (!busy) {
                                busy = true
                                msg = "转存中…"
                                scope.launch {
                                    runCatching {
                                        withContext(Dispatchers.IO) {
                                            api.fetchEpisode(tmdbId, season, ep.episode, src.index)
                                        }
                                    }.onFailure { msg = it.message ?: "转存失败" }
                                    load(refresh = true)
                                    busy = false
                                }
                            }
                        },
                    )
                }
                items(w.notes) { Sub("· $it", 13) }
            }
        }
    }
}

private suspend fun pollJob(api: Api, job: String, onStep: (String) -> Unit) {
    var shown = 0
    repeat(300) {
        val st = api.jobStatus(job)
        st.steps.drop(shown).forEach(onStep)
        shown = st.steps.size
        if (st.done) {
            if (st.error.isNotEmpty()) onStep("✗ ${st.error}")
            return
        }
        kotlinx.coroutines.delay(1000)
    }
    onStep("超时")
}

@Composable
private fun EpisodeRow(
    work: Work,
    ep: Episode,
    onPlay: (Copy) -> Unit,
    onFetch: (Source) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Box(Modifier.width(70.dp)) {
            Text(
                if (work.isMovie) "影片" else "E%02d".format(ep.episode),
                color = if (ep.status == "saved") T.Accent else T.Dim, fontSize = 18.sp,
            )
        }
        Column(Modifier.weight(1f)) {
            Text(
                ep.title.ifEmpty { if (work.isMovie) work.title else "第 ${ep.episode} 集" },
                color = T.Text, fontSize = 18.sp, maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            val bits = buildList {
                ep.local?.let { c ->
                    add((if (c.height > 0) "${c.height}p · " else "") + c.sizeH)
                    if (ep.copies.size > 1) add("${ep.copies.size} 个版本")
                    if (!c.transcodable) add("无转码档")
                }
                if (ep.local == null && ep.sources.isNotEmpty()) {
                    add("${ep.sources.size} 个来源可补")
                }
                ep.watched?.takeIf { !it.finished && it.percent > 0 }
                    ?.let { add("看到 ${it.percent}%") }
                if (ep.airDate.isNotEmpty()) add(ep.airDate)
            }
            Sub(bits.joinToString("　·　"), 13)
        }
        // 每一份副本一个按钮：电视上「换来源」就是往右按一格，比菜单快
        ep.copies.forEachIndexed { i, c ->
            TvButton(
                text = if (ep.copies.size == 1) {
                    if (ep.watched?.finished == false && ep.watched.resumeAt > 0) "继续" else "播放"
                } else {
                    (if (c.height > 0) "${c.height}p" else "版本${i + 1}") +
                        (if (!c.transcodable) "!" else "")
                },
            ) { onPlay(c) }
        }
        if (ep.copies.isEmpty()) {
            ep.sources.take(3).forEach { s ->
                TvButton(s.label + (if (s.height > 0) " ${s.height}p" else "")) { onFetch(s) }
            }
        }
    }
}
