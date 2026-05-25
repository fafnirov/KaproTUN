import org.gradle.api.tasks.Copy

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

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

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
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
    dependsOn(copyDefaultSitesJson)
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.androidx.datastore.preferences)
    implementation(libs.kotlinx.serialization.json)

    testImplementation(libs.junit)
}
