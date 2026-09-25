package com.mediafans.tv

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

/** 界面栈。没上 navigation 库——四个页面，一个密封类比一套路由更好读。 */
sealed interface Screen {
    data object Home : Screen
    data object Search : Screen
    /** [notice]：为什么被送回这一页（比如登录过期），显示在表单上方。 */
    data class Setup(val notice: String = "") : Screen
    /**
     * [season]：从「继续观看」「片库」进来时直接落在看到的那一季，
     * 不然看到第三季的人每次进来都得先从第一季翻过去。
     * [nd]：这部作品的文件在哪个盘。空 = 跟随当前设置。
     */
    data class Detail(
        val tmdbId: Int, val media: String, val title: String,
        val season: Int? = null, val nd: String = "",
    ) : Screen
}

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = Settings(this)
        val api = Api(settings)
        setContent {
            var stack by remember {
                mutableStateOf(
                    // 没配服务器就先去设置页，不然首页只会是一屏报错
                    listOf<Screen>(if (settings.configured) Screen.Home else Screen.Setup())
                )
            }
            val push: (Screen) -> Unit = { stack = stack + it }
            val pop: () -> Unit = { if (stack.size > 1) stack = stack.dropLast(1) }
            // 令牌失效（过期 / 改密码被作废 / 管理员删了账号）：整个栈清掉回登录页。
            // 留着旧栈没有意义，退回去的每一页都会再报一次「需要登录」。
            val authLost: (String) -> Unit = { msg ->
                settings.token = ""
                settings.user = ""
                stack = listOf(Screen.Setup(msg))
            }

            Box(Modifier.fillMaxSize().background(T.Bg)) {
                when (val cur = stack.last()) {
                    is Screen.Setup -> SetupScreen(api, settings, cur.notice) {
                        stack = listOf(Screen.Home)
                    }
                    is Screen.Home -> HomeScreen(
                        api = api,
                        settings = settings,
                        onOpen = {
                            push(Screen.Detail(it.tmdbId, it.mediaType, it.title, season = it.season))
                        },
                        onOpenLib = { l ->
                            push(Screen.Detail(l.tmdbId, l.mediaType, l.title,
                                season = l.watchedSeason ?: l.season))
                        },
                        onResume = { r -> openFromRecent(r, push) },
                        onSearch = { push(Screen.Search) },
                        onSetup = { push(Screen.Setup()) },
                        onAuthLost = authLost,
                    )
                    is Screen.Search -> SearchScreen(api) {
                        push(Screen.Detail(it.tmdbId, it.mediaType, it.title))
                    }
                    is Screen.Detail -> {
                        val nd = cur.nd.ifEmpty { settings.netdisk }
                        DetailScreen(
                            api = api,
                            settings = settings,
                            tmdbId = cur.tmdbId,
                            media = cur.media,
                            titleHint = cur.title,
                            initialSeason = cur.season,
                            nd = nd,
                            onPlay = { work, ep, copy, startAt ->
                                startActivity(PlayerActivity.intent(
                                    this@MainActivity, work, ep, copy, startAt, nd))
                            },
                            onAuthLost = authLost,
                        )
                    }
                }
                // 遥控器返回键：栈里还有上一页就退一页，否则交给系统退出应用
                BackHandler(enabled = stack.size > 1) { pop() }
            }
        }
    }

    /**
     * 「继续观看」点进去：剧集回作品页接着播，散片直接播。
     *
     * 两样都得带上**记录里的网盘**，不能用当前设置：在百度盘上看的，
     * 切到夸克后再点，按夸克去找那个路径只会是「文件不存在」。
     */
    private fun openFromRecent(r: RecentItem, push: (Screen) -> Unit) {
        if (r.tmdbId > 0) {
            push(Screen.Detail(r.tmdbId, r.mediaType, r.title,
                season = r.season, nd = r.netdisk))
        } else {
            startActivity(PlayerActivity.intentForPath(
                this, r.playPath, r.name, r.position.toLong() * 1000, r.netdisk))
        }
    }
}
