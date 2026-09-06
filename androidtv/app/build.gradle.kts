plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.mediafans.tv"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.mediafans.tv"
        // 电视盒子的系统普遍偏旧，minSdk 往低了压。ExoPlayer 和 Compose 都支持到 21，
        // 这里取 23 是因为 tv-material 的最低要求。
        minSdk = 23
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // 没有发布签名时用 debug 签名：电视上装的是侧载 APK，
            // 有签名就能装，不签名装不了。
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

    buildFeatures { compose = true }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.09.00")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    // 刻意不引 androidx.tv:tv-material：它的焦点语义是好，但 1.0.0 前后
    // TvLazyRow 之类的 API 挪过位置，赌错就是一堆编译错误。焦点这块用
    // foundation 的 focusable/onFocusChanged 自己控，行为完全可预期。

    // 播放器。用原生解码是这个 app 存在的主要理由之一：
    // 电视盒子能硬解 HEVC/DTS-HD 的原盘 MKV，浏览器不能。
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    // 夸克的转码档是 m3u8。少了这个模块，一旦回退到转码档就是
    // ClassNotFoundException: HlsMediaSource$Factory —— 直接崩，不是播放失败。
    implementation("androidx.media3:media3-exoplayer-hls:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")

    implementation("io.coil-kt:coil-compose:2.7.0")
}
