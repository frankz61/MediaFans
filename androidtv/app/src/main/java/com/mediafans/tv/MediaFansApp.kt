package com.mediafans.tv

import android.app.Application
import android.os.Build
import coil.ImageLoader
import coil.ImageLoaderFactory
import okhttp3.OkHttpClient
import java.security.KeyStore
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * 只为一件事存在：让老电视盒子认 Let's Encrypt 的证书。
 *
 * ISRG 根要到 Android 7.1.1 才进系统证书库，而电视盒子大量停在 6.0/7.0。
 * 7.0 起 res/xml/network_security_config.xml 已经把打包的根加进来了；
 * 6.0 不读那个文件，只能在这里把同一批根装进两处默认的 TLS 配置：
 *  - HttpsURLConnection：Api 和 ExoPlayer 的 DefaultHttpDataSource 都走它；
 *  - Coil 的 OkHttp：海报图。
 * 系统证书库照样先用，打包的根只是兜底。
 */
class MediaFansApp : Application(), ImageLoaderFactory {

    private var legacyTls: Pair<SSLContext, X509TrustManager>? = null

    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N) {
            // 装不上就算了：退回系统默认，顶多还是连不上，不至于启动就崩
            legacyTls = runCatching { buildTls() }.getOrNull()?.also { (ctx, _) ->
                HttpsURLConnection.setDefaultSSLSocketFactory(ctx.socketFactory)
            }
        }
    }

    override fun newImageLoader(): ImageLoader {
        val b = ImageLoader.Builder(this)
        legacyTls?.let { (ctx, tm) ->
            b.okHttpClient {
                OkHttpClient.Builder().sslSocketFactory(ctx.socketFactory, tm).build()
            }
        }
        return b.build()
    }

    private fun buildTls(): Pair<SSLContext, X509TrustManager> {
        val cf = CertificateFactory.getInstance("X.509")
        val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null, null) }
        BUNDLED_ROOTS.forEachIndexed { i, res ->
            resources.openRawResource(res).use { ks.setCertificateEntry("root$i", cf.generateCertificate(it)) }
        }
        val tm = FallbackTrustManager(trustManagerOf(null), trustManagerOf(ks))
        val ctx = SSLContext.getInstance("TLS").apply { init(null, arrayOf(tm), null) }
        return ctx to tm
    }

    private fun trustManagerOf(ks: KeyStore?): X509TrustManager =
        TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
            .apply { init(ks) }
            .trustManagers.filterIsInstance<X509TrustManager>().first()

    /** 先问系统证书库，不认再问打包的根。 */
    private class FallbackTrustManager(
        private val system: X509TrustManager,
        private val bundled: X509TrustManager,
    ) : X509TrustManager {
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            try {
                system.checkServerTrusted(chain, authType)
            } catch (e: CertificateException) {
                bundled.checkServerTrusted(chain, authType)
            }
        }

        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) =
            system.checkClientTrusted(chain, authType)

        override fun getAcceptedIssuers(): Array<X509Certificate> =
            system.acceptedIssuers + bundled.acceptedIssuers
    }

    companion object {
        /** 跟 network_security_config.xml 里的列表保持一致。 */
        private val BUNDLED_ROOTS = intArrayOf(
            R.raw.isrg_root_x1, R.raw.isrg_root_x2, R.raw.isrg_root_ye, R.raw.isrg_root_yr,
        )
    }
}
