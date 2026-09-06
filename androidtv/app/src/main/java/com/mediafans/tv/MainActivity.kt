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
    data object Setup : Screen
    data class Detail(val tmdbId: Int, val media: String, val title: String) : Screen
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
                    listOf<Screen>(if (settings.configured) Screen.Home else Screen.Setup)
                )
            }
            val push: (Screen) -> Unit = { stack = stack + it }
            val pop: () -> Unit = { if (stack.size > 1) stack = stack.dropLast(1) }

            Box(Modifier.fillMaxSize().background(T.Bg)) {
                when (val cur = stack.last()) {
                    is Screen.Setup -> SetupScreen(settings) {
                        stack = listOf(Screen.Home)
                    }
                    is Screen.Home -> HomeScreen(
                        api = api,
                        settings = settings,
                        onOpen = { push(Screen.Detail(it.tmdbId, it.mediaType, it.title)) },
                        onResume = { r -> openFromRecent(api, r, push) },
                        onSearch = { push(Screen.Search) },
                        onSetup = { push(Screen.Setup) },
                    )
                    is Screen.Search -> SearchScreen(api) {
                        push(Screen.Detail(it.tmdbId, it.mediaType, it.title))
                    }
                    is Screen.Detail -> DetailScreen(
                        api = api,
                        tmdbId = cur.tmdbId,
                        media = cur.media,
                        titleHint = cur.title,
                        onPlay = { work, ep, copy, startAt ->
                            startActivity(PlayerActivity.intent(
                                this@MainActivity, work, ep, copy, startAt))
                        },
                    )
                }
                // 遥控器返回键：栈里还有上一页就退一页，否则交给系统退出应用
                BackHandler(enabled = stack.size > 1) { pop() }
            }
        }
    }

    /** 「继续观看」点进去：剧集回作品页接着播，散片直接播。 */
    private fun openFromRecent(api: Api, r: RecentItem, push: (Screen) -> Unit) {
        if (r.tmdbId > 0) {
            push(Screen.Detail(r.tmdbId, r.mediaType, r.title))
        } else {
            startActivity(PlayerActivity.intentForPath(
                this, r.playPath, r.name, r.position.toLong() * 1000))
        }
    }
}
