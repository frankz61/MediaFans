package com.mediafans.tv

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.dp

/** 主题色，跟网页端一致，电视上看着不会像两个应用。 */
object T {
    val Bg = Color(0xFF0F1218)
    val Panel = Color(0xFF161B24)
    val Line = Color(0xFF2A3140)
    val Text = Color(0xFFE8ECF4)
    val Dim = Color(0xFF8A93A6)
    val Accent = Color(0xFF4C8DFF)
    val Warn = Color(0xFFFFC46B)
    val Err = Color(0xFFFF7A7A)
}

/**
 * 一个能被 D-pad 选中的块。
 *
 * 电视端和手机端最大的差别是**没有指针**：用户永远只能看到「焦点在哪」。
 * 所以焦点态必须同时给三个信号——放大、描边、底色变亮，缺一个在一米开外都看不清。
 *
 * 遥控器的「确定」键（DPAD_CENTER / ENTER）由 clickable 处理，不用自己收键。
 */
@Composable
fun FocusBox(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(10.dp),
    focusedScale: Float = 1.08f,
    onFocused: (() -> Unit)? = null,
    content: @Composable BoxScope.(focused: Boolean) -> Unit,
) {
    var focused by remember { mutableStateOf(false) }
    val scale by animateFloatAsState(if (focused) focusedScale else 1f, label = "scale")
    val interaction = remember { MutableInteractionSource() }
    Box(
        modifier = modifier
            .scale(scale)
            .clip(shape)
            .border(
                BorderStroke(if (focused) 3.dp else 1.dp, if (focused) T.Accent else T.Line),
                shape,
            )
            .onFocusChanged {
                focused = it.isFocused
                if (it.isFocused) onFocused?.invoke()
            }
            .focusable(interactionSource = interaction)
            // clickable 放在 focusable 之后：这样它只在拿到焦点时响应确定键，
            // 而不是把整块变成一个会抢焦点的独立目标
            .clickable(interactionSource = interaction, indication = null, onClick = onClick),
    ) {
        content(focused)
    }
}
