import org.gradle.api.tasks.Copy
import java.net.URI
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// --------------------------------------------------------------------------
// Release signing — читаем keystore-параметры из keystore.properties в корне
// android/. Файл gitignored (содержит пароли). Если файла нет, release
// собирается debug-ключом (для personal use OK, но overwrite-апдейт через
// Play не получится — нужен консистентный keystore).
//
// Создать keystore (один раз):
//   keytool -genkey -v -keystore kaprovpn-release.jks -keyalg RSA \
//     -keysize 4096 -validity 10000 -alias kaprovpn
// Потом скопировать `keystore.properties.example` → `keystore.properties`,
// заполнить пути / пароли.
// --------------------------------------------------------------------------
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) {
        keystorePropsFile.inputStream().use { load(it) }
    }
}
val hasReleaseSigning = keystoreProps.getProperty("storeFile") != null

android {
    namespace = "pro.kaprovpn.android"
    compileSdk = 34

    defaultConfig {
        applicationId = "pro.kaprovpn.android"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-dev"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // R8: shrink + obfuscate + optimize. Без этого debug-сборка =
            // release-сборка (150MB и весь dead code из icons-extended).
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Release-signing если keystore.properties задан, иначе debug-key.
            signingConfig = if (hasReleaseSigning)
                signingConfigs.getByName("release")
            else
                signingConfigs.getByName("debug")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    // ABI splits — отдельный APK на каждую архитектуру вместо одного fat
    // APK со всеми 4 .so внутри. Уменьшает download size с ~150MB до
    // ~40-50MB на устройство. Universal APK (со всеми ABI) тоже выдаётся
    // для прямой раздачи когда не знаешь ABI приёмника.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
            isUniversalApk = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    sourceSets {
        named("main") {
            java.srcDirs("src/main/kotlin")
            assets.srcDir(layout.buildDirectory.dir("generated/assets"))
        }
        named("test") {
            java.srcDirs("src/test/kotlin")
        }
    }
}

// Single source of truth: the desktop client and the Android client both read
// the same default_sites.json. We sync it from kapro_vpn/data/ into Android
// assets at build time so there's no manual copy-paste.
val copyDefaultSitesJson by tasks.registering(Copy::class) {
    description = "Sync default_sites.json from monorepo root into assets"
    from(rootProject.layout.projectDirectory.dir("../kapro_vpn/data").file("default_sites.json"))
    into(layout.buildDirectory.dir("generated/assets"))
}

tasks.named("preBuild") {
    dependsOn(copyDefaultSitesJson, downloadLibV2ray)
}

// --------------------------------------------------------------------------
// libv2ray.aar — prebuilt Xray-core bindings из 2dust/AndroidLibXrayLite.
//
// Слишком тяжёлый для git (~55 MB) — gitignore'нится. Этот task скачивает
// файл на первый build (~30 сек) и кеширует локально. На follow-up билдах —
// no-op (UP-TO-DATE) если файл уже есть.
//
// Версия совпадает с тегом релиза 2dust/AndroidLibXrayLite. Обновление:
// бумпнуть `libV2rayVersion`, удалить app/libs/libv2ray.aar, пересобрать.
// --------------------------------------------------------------------------
val libV2rayVersion = "v26.5.19"
val libV2rayFile = layout.projectDirectory.file("libs/libv2ray.aar").asFile

val downloadLibV2ray by tasks.registering {
    description = "Скачать libv2ray.aar $libV2rayVersion из GitHub releases если отсутствует"
    outputs.file(libV2rayFile)
    doLast {
        if (libV2rayFile.exists() && libV2rayFile.length() > 50_000_000L) {
            logger.lifecycle("libv2ray.aar уже скачан (${libV2rayFile.length() / 1024 / 1024} MB) — skip")
            return@doLast
        }
        libV2rayFile.parentFile.mkdirs()
        val url = "https://github.com/2dust/AndroidLibXrayLite/releases/download/$libV2rayVersion/libv2ray.aar"
        logger.lifecycle("Скачиваю libv2ray.aar $libV2rayVersion ($url)…")
        URI(url).toURL().openStream().use { input ->
            libV2rayFile.outputStream().use { output -> input.copyTo(output) }
        }
        logger.lifecycle("OK — ${libV2rayFile.length() / 1024 / 1024} MB")
    }
}

dependencies {
    // VPN engine — Xray-core через gomobile-сгенерированные JNI-биндинги.
    // Файл качается downloadLibV2ray task'ом (см. выше).
    implementation(files(libV2rayFile))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    // LocalLifecycleOwner в Compose был частью compose-ui, но deprecated с
    // Compose 1.7+. Современный — из lifecycle-runtime-compose. Нужен для
    // ScanQrScreen, который привязывает CameraX к LifecycleOwner.
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    // material-icons-extended ~3-4 MB, но R8 tree-shake'ит неиспользуемые
    // в release. Используется для CloudDownload и других иконок, которых
    // нет в material-icons-core (Home/List/Settings/Add/Check/Delete).
    implementation(libs.androidx.compose.material.icons.extended)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.kotlinx.serialization.json)

    // QR-сканер на ConfigsScreen → ScanQrScreen. CameraX даёт PreviewView +
    // ImageAnalysis use-case, ML Kit barcode жуёт байты с analyzer'а и
    // достаёт share-URL. Bundled-вариант ML Kit (~2.5 MB к APK) — модель
    // уже внутри AAR, не дёргает Play Services, работает offline на
    // устройствах без GMS.
    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)
    implementation(libs.mlkit.barcode.scanning)

    // ZXing для генерации QR в ShareConfigDialog (обратная операция к
    // сканеру выше). Только pure-Java core, без android-extras и
    // embedded-reader — у нас уже есть свой reader через ML Kit + CameraX.
    implementation(libs.zxing.core)

    testImplementation(libs.junit)
}
