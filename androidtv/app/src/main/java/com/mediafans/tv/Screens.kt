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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
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
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
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

// ---------------------------------------------------------------- 设置 / 登录

/**
 * 连接服务器 + 登录。
 *
 * 两种登录方式：
 * - **账号**（默认）：服务端开了多用户之后，家里每个人一个账号，片库和进度按人分。
 *   电视是全家共用的，但「继续观看」要的恰恰是**我**看到哪了。
 * - **访问令牌**：服务端 `--token` 那个共享令牌，视为管理员。升级前的电视都是这么连的，
 *   不能让它们升级完就连不上。
 *
 * 保存前先真的连一次：地址填错、入口路径抄进来、密码不对，都在这一页当场说清楚，
 * 而不是进了首页才是一屏莫名其妙的报错。
 */
@Composable
fun SetupScreen(api: Api, settings: Settings, notice: String, onDone: () -> Unit) {
    var base by remember { mutableStateOf(settings.base) }
    // 已经存着令牌、又不是真账号的（共享令牌，或老版本升级上来的）：默认停在令牌方式
    var byToken by remember {
        mutableStateOf(settings.token.isNotEmpty() && !settings.hasAccount)
    }
    var username by remember {
        mutableStateOf(if (settings.hasAccount) settings.user else "")
    }
    var password by remember { mutableStateOf("") }
    var token by remember { mutableStateOf(if (byToken) settings.token else "") }
    var nd by remember { mutableStateOf(settings.netdisk) }
    var msg by remember { mutableStateOf(notice) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }

    val loggedIn = settings.token.isNotEmpty()

    val connect: () -> Unit = connect@{
        if (busy) return@connect
        val (addr, tokInAddr) = splitAddress(base)
        if (addr.isEmpty()) { msg = "先填服务器地址"; return@connect }
        // 地址栏里带着 token=：直接当令牌登录，省得再抄一遍
        val useToken = byToken || (tokInAddr.isNotEmpty() && username.isBlank())
        val tok = token.trim().ifEmpty { tokInAddr }
        if (useToken && tok.isEmpty()) { msg = "填访问令牌，或者换成账号登录"; return@connect }
        if (!useToken && (username.isBlank() || password.isEmpty())) {
            msg = "填用户名和密码"; return@connect
        }
        busy = true
        msg = "连接中…"
        val old = Triple(settings.base, settings.token, settings.user)
        scope.launch {
            runCatching {
                withContext(Dispatchers.IO) {
                    val real = api.resolveBase(addr)
                    val newToken = if (useToken) tok
                                   else api.login(real, username.trim(), password)
                    settings.base = real
                    settings.token = newToken
                    // 顺带验一次：令牌填错在这里就会 401，而不是进了首页才报
                    settings.user = api.me().name
                }
            }.onSuccess {
                settings.netdisk = nd
                busy = false
                onDone()
            }.onFailure {
                // 没连上就别动原来那套：可能只是这次手滑，旧的还能用
                settings.base = old.first
                settings.token = old.second
                settings.user = old.third
                busy = false
                msg = (it as? ApiError)?.takeIf { e -> e.needLogin && useToken }
                    ?.let { "访问令牌不对" } ?: (it.message ?: "连接失败")
            }
        }
    }

    // 这一页在手机上会被输入法吃掉大半高度（横屏尤其惨），所以：
    // 能滚 + 顶对齐 + imePadding。原来是垂直居中且不可滚，输入法一弹，
    // 「保存并进入」直接被顶到屏幕外，看起来就像没有确认按钮。
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .imePadding()
            .padding(OVERSCAN),
    ) {
        Title("连接到 MediaFans 服务", 26)
        Spacer(Modifier.height(4.dp))
        Sub("填网页端那台服务的地址，用你的账号登录。填一次就够了。")
        if (loggedIn) {
            Spacer(Modifier.height(4.dp))
            Sub("当前：" + (if (settings.hasAccount) settings.user else "访问令牌（管理员）") +
                "　@ " + settings.base, 14, T.Accent)
        }
        if (msg.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Sub(msg, 15, if (busy) T.Dim else T.Warn)
        }
        Spacer(Modifier.height(16.dp))

        Field("服务器地址（网页端地址栏整个抄过来也行）", base,
              "https://example.com:12583", first, imeAction = ImeAction.Next) { base = it }
        Spacer(Modifier.height(12.dp))

        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Sub("登录方式", 16, T.Text)
            TvButton(if (!byToken) "● 账号" else "○ 账号") { byToken = false }
            TvButton(if (byToken) "● 访问令牌" else "○ 访问令牌") { byToken = true }
        }
        Spacer(Modifier.height(12.dp))

        if (byToken) {
            // 令牌这一栏的键盘上给「完成」：手机横屏时按钮多半在屏幕外，
            // 键盘上那个对勾才是最先够得着的确认入口
            Field("访问令牌", token, "服务端 --token 那一串（管理员，没有个人片库）",
                  imeAction = ImeAction.Done, onDone = connect) { token = it }
        } else {
            Field("用户名", username, "管理员在网页端「用户」里给你开的账号",
                  imeAction = ImeAction.Next) { username = it }
            Spacer(Modifier.height(12.dp))
            Field("密码", password, "", password = true,
                  imeAction = ImeAction.Done, onDone = connect) { password = it }
        }
        Spacer(Modifier.height(16.dp))

        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Sub("网盘", 16, T.Text)
            TvButton(if (nd == "quark") "● 夸克" else "○ 夸克") { nd = "quark" }
            TvButton(if (nd == "baidu") "● 百度" else "○ 百度") { nd = "baidu" }
        }
        Spacer(Modifier.height(16.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TvButton(if (busy) "连接中…" else "登录并进入", onClick = connect)
            if (loggedIn) {
                TvButton("退出登录") {
                    if (!busy) {
                        busy = true
                        scope.launch {
                            withContext(Dispatchers.IO) { api.logout() }
                            settings.token = ""
                            settings.user = ""
                            token = ""
                            password = ""
                            busy = false
                            msg = "已退出"
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun Field(
    label: String,
    value: String,
    hint: String,
    focusRequester: FocusRequester? = null,
    imeAction: ImeAction = ImeAction.Default,
    onDone: (() -> Unit)? = null,
    password: Boolean = false,
    onChange: (String) -> Unit,
) {
    Column {
        if (label.isNotEmpty()) {
            Sub(label, 14)
            Spacer(Modifier.height(4.dp))
        }
        var focused by remember { mutableStateOf(false) }
        BasicTextField(
            value = value,
            onValueChange = onChange,
            singleLine = true,
            textStyle = TextStyle(color = T.Text, fontSize = 18.sp),
            keyboardOptions = KeyboardOptions(
                imeAction = imeAction,
                keyboardType = if (password) KeyboardType.Password else KeyboardType.Text,
            ),
            keyboardActions = KeyboardActions(onDone = { onDone?.invoke() }),
            visualTransformation = if (password) PasswordVisualTransformation()
                                   else VisualTransformation.None,
            cursorBrush = androidx.compose.ui.graphics.SolidColor(T.Accent),
            modifier = (focusRequester?.let { Modifier.focusRequester(it) } ?: Modifier)
                // 窄屏上写死 700dp 会横向溢出，取两者较小
                .fillMaxWidth()
                .widthIn(max = 700.dp)
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
    onOpenLib: (LibItem) -> Unit,
    onResume: (RecentItem) -> Unit,
    onSearch: () -> Unit,
    onSetup: () -> Unit,
    onAuthLost: (String) -> Unit,
) {
    var recent by remember { mutableStateOf<List<RecentItem>>(emptyList()) }
    var library by remember { mutableStateOf<List<LibItem>>(emptyList()) }
    val rails = remember { mutableStateMapOf<String, List<MediaItem>>() }
    var err by remember { mutableStateOf("") }
    // 网盘切换就在首页上点，不用进设置页：Settings 不是 Compose 状态，
    // 拿一份本地状态镜像它，切换时才会触发重组和重新加载
    var nd by remember { mutableStateOf(settings.netdisk) }
    val first = remember { FocusRequester() }

    // 进度和片库都跟人走，不跟网盘走；这里只在进首页时拉一次。
    // 从播放页回来时 HomeScreen 没有被销毁（播放是另一个 Activity），
    // 所以还要在 ON_RESUME 时再拉——不然刚看完的那集不会出现在「继续观看」里
    var reloadTick by remember { mutableStateOf(0) }
    val owner = androidx.lifecycle.compose.LocalLifecycleOwner.current
    androidx.compose.runtime.DisposableEffect(owner) {
        val obs = androidx.lifecycle.LifecycleEventObserver { _, e ->
            if (e == androidx.lifecycle.Lifecycle.Event.ON_RESUME) reloadTick++
        }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }
    LaunchedEffect(reloadTick) {
        // 0 是注册观察者之前的初值；注册时会立刻补发一次 ON_RESUME 把它变成 1，
        // 从那次开始拉，免得首屏同一批请求发两遍
        if (reloadTick == 0) return@LaunchedEffect
        runCatching { withContext(Dispatchers.IO) { api.recent(20) } }
            .onSuccess { recent = it; err = "" }
            .onFailure {
                if (it is ApiError && it.needLogin) { onAuthLost("登录已失效，请重新登录"); return@LaunchedEffect }
                err = it.message ?: "读取最近观看失败"
            }
        // 老版本升级上来的只存了令牌、不知道自己是谁：补问一次，
        // 是真账号就能显示片库
        if (settings.user.isEmpty() && settings.token.isNotEmpty()) {
            runCatching { withContext(Dispatchers.IO) { api.me() } }
                .onSuccess { settings.user = it.name }
        }
        if (settings.hasAccount) {
            runCatching { withContext(Dispatchers.IO) { api.library() } }
                .onSuccess { library = it }
        }
    }

    LaunchedEffect(Unit) {
        // 每条榜单单独取、单独失败：TMDB 某一档挂了不该让整个首页空着
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
                TvButton(if (nd == "baidu") "百度网盘 ⇄" else "夸克网盘 ⇄") {
                    nd = if (nd == "baidu") "quark" else "baidu"
                    settings.netdisk = nd
                }
                TvButton(if (settings.hasAccount) settings.user else "账号", onClick = onSetup)
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
        if (library.isNotEmpty()) {
            item {
                Column {
                    Title("我的片库", 22)
                    Spacer(Modifier.height(10.dp))
                    LazyRow(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        items(library, key = { "${it.mediaType}:${it.tmdbId}" }) {
                            LibCard(it, onOpenLib)
                        }
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
private fun LibCard(l: LibItem, onOpen: (LibItem) -> Unit) {
    Column(Modifier.width(150.dp)) {
        FocusBox(onClick = { onOpen(l) }) { _ ->
            Box(Modifier.fillMaxWidth().aspectRatio(2f / 3f).background(T.Panel)) {
                if (l.poster.isNotEmpty()) {
                    AsyncImage(l.poster, l.title, Modifier.fillMaxSize(),
                        contentScale = ContentScale.Crop)
                }
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(l.title, color = T.Text, fontSize = 14.sp, maxLines = 1,
            overflow = TextOverflow.Ellipsis)
        Sub(
            when {
                l.watchedSeason != null && l.watchedEpisode != null ->
                    "看到 S${l.watchedSeason}E${l.watchedEpisode}" +
                        (if (l.finished) " ✓" else if (l.percent > 0) " · ${l.percent}%" else "")
                l.mediaType == "movie" -> listOf(l.year, "电影").filter { it.isNotEmpty() }
                    .joinToString("　")
                else -> l.year
            },
            12,
        )
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
    settings: Settings,
    tmdbId: Int,
    media: String,
    titleHint: String,
    initialSeason: Int?,
    nd: String,
    onPlay: (Work, Episode, Copy, Long) -> Unit,
    onAuthLost: (String) -> Unit,
) {
    var season by remember {
        mutableStateOf(if (media == "movie") 0 else (initialSeason?.takeIf { it > 0 } ?: 1))
    }
    var work by remember { mutableStateOf<Work?>(null) }
    var msg by remember { mutableStateOf("读取中…") }
    var busy by remember { mutableStateOf(false) }
    var inLibrary by remember { mutableStateOf<Boolean?>(null) }
    val scope = rememberCoroutineScope()
    val first = remember { FocusRequester() }

    suspend fun load(refresh: Boolean = false) {
        runCatching {
            withContext(Dispatchers.IO) { api.work(tmdbId, season, media, refresh, nd) }
        }
            .onSuccess { work = it; msg = "" }
            .onFailure {
                if (it is ApiError && it.needLogin) onAuthLost("登录已失效，请重新登录")
                else msg = it.message ?: "读取失败"
            }
    }
    LaunchedEffect(tmdbId, season) { load() }
    // 播完回到这一页时刷新一下，「看到 xx%」「继续」才是新的
    val owner = androidx.lifecycle.compose.LocalLifecycleOwner.current
    androidx.compose.runtime.DisposableEffect(owner) {
        // 注册观察者时会立刻补发一次 ON_RESUME，那次跳过——首次加载由上面的 effect 管
        var initial = true
        val obs = androidx.lifecycle.LifecycleEventObserver { _, e ->
            if (e == androidx.lifecycle.Lifecycle.Event.ON_RESUME) {
                if (initial) initial = false else scope.launch { load() }
            }
        }
        owner.lifecycle.addObserver(obs)
        onDispose { owner.lifecycle.removeObserver(obs) }
    }
    LaunchedEffect(tmdbId) {
        if (settings.hasAccount) {
            runCatching { withContext(Dispatchers.IO) { api.library() } }
                .onSuccess { lib ->
                    inLibrary = lib.any { it.tmdbId == tmdbId && it.mediaType == media }
                }
        }
    }
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
                // 「继续」放第一个、拿初始焦点：从「继续观看」点进来的人，
                // 进页面按一下确定就该接着播，而不是先找到那一集
                val resume = resumeTarget(w)
                if (resume != null) {
                    val (ep, copy) = resume
                    val at = ep.watched
                        ?.takeIf { !it.finished && it.path == copy.path }?.resumeAt ?: 0
                    TvButton(
                        (if (at > 0) "继续 " else "播放 ") +
                            (if (w.isMovie) "" else "E%02d".format(ep.episode)),
                        focusRequester = first,
                    ) { onPlay(w, ep, copy, at.toLong() * 1000) }
                }
                // 初始焦点挂在按钮上而不是第一集：LaunchedEffect 跑的时候
                // LazyColumn 还没排出第一项，requestFocus 会静默失败，
                // 结果就是进页面后遥控器按什么都没反应
                TvButton(if (w.isMovie) "找资源" else "找缺失的集",
                         focusRequester = if (resume == null) first else null) {
                    if (!busy) {
                        busy = true
                        msg = "搜索中…"
                        scope.launch {
                            runCatching {
                                withContext(Dispatchers.IO) {
                                    val job = api.scan(tmdbId, season, media, nd)
                                    pollJob(api, job) { msg = it }
                                }
                            }.onFailure { msg = it.message ?: "搜索失败" }
                            load(refresh = true)
                            busy = false
                        }
                    }
                }
                // 片库只对真账号有意义：共享令牌是内建管理员，不在账号表里
                inLibrary?.let { inLib ->
                    TvButton(if (inLib) "✓ 在片库里" else "＋ 加入片库") {
                        if (!busy) {
                            busy = true
                            scope.launch {
                                runCatching {
                                    withContext(Dispatchers.IO) {
                                        if (inLib) api.libraryRemove(w.tmdbId, w.mediaType)
                                        else api.libraryAdd(w)
                                    }
                                }.onSuccess { inLibrary = !inLib }
                                    .onFailure { msg = it.message ?: "操作失败" }
                                busy = false
                            }
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
                                            api.fetchEpisode(tmdbId, season, ep.episode, src.index, nd)
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

/**
 * 「继续」该播哪一集：看过的里集数最大的那一集；它已经看完了就接下一集
 * （得是存好了的）。电影就是那唯一一行。都没有就返回 null，不显示这个按钮。
 *
 * 取「集数最大」而不是「最近看的」：这一页的数据里没有观看时间，
 * 而追剧的人几乎总是往后看，集数最大就是最近看的。
 */
private fun resumeTarget(w: Work): Pair<Episode, Copy>? {
    val watched = w.episodes.filter { it.watched != null && it.local != null }
    val last = watched.maxByOrNull { it.episode } ?: return null
    if (last.watched?.finished != true) {
        val copy = last.copies.firstOrNull { it.path == last.watched?.path } ?: last.local!!
        return last to copy
    }
    val next = w.episodes.filter { it.episode > last.episode && it.local != null }
        .minByOrNull { it.episode } ?: return null
    return next to next.local!!
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
