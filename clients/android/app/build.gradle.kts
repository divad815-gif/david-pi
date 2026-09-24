import java.util.Properties
import java.nio.file.Files
import java.nio.file.LinkOption

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

val sha256Pattern = Regex("^[0-9a-f]{64}$")
val releaseSourceSha256 = providers.environmentVariable("DAVID_PI_ANDROID_SOURCE_SHA256")
val repositoryRoot = rootProject.projectDir.parentFile.parentFile.canonicalFile.toPath()
val signingFileCandidate = providers.environmentVariable("DAVID_PI_ANDROID_KEYSTORE_PROPERTIES")
    .orNull?.let(::file) ?: rootProject.file("keystore.properties")
var externalReleaseSigningConfigured = false

fun verifiedExternalSigningFile(candidate: File): File {
    val absolute = candidate.absoluteFile
    val unresolved = absolute.toPath()
    if (
        Files.isSymbolicLink(unresolved)
        || !Files.isRegularFile(unresolved, LinkOption.NOFOLLOW_LINKS)
    ) {
        throw GradleException("Android release signing inputs must be regular files")
    }
    val canonical = absolute.canonicalFile
    if (canonical.toPath().startsWith(repositoryRoot)) {
        throw GradleException("Android release signing inputs must stay outside the repository")
    }
    return canonical
}

android {
    namespace = "com.davidpi.backup"
    compileSdk = 35
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "com.davidpi.backup"
        minSdk = 26
        targetSdk = 35
        versionCode = 27
        versionName = "1.2.0-portable-households"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

    }

    // Release credentials stay outside the repository. A migrated properties
    // file may retain a machine-specific storeFile value, so CI/recovery hosts
    // can override paths without copying or rewriting any secret material.
    if (signingFileCandidate.exists()) {
        val signingFile = verifiedExternalSigningFile(signingFileCandidate)
        val signing = Properties().apply { signingFile.inputStream().use(::load) }
        fun requiredSigningValue(name: String): String =
            signing.getProperty(name)?.takeIf { it.isNotBlank() }
                ?: throw GradleException("Android release signing properties are incomplete")
        val signingStore = providers.environmentVariable("DAVID_PI_ANDROID_KEYSTORE_FILE").orNull
            ?: requiredSigningValue("storeFile")
        val rawStoreFile = File(signingStore)
        val resolvedStoreFile = if (rawStoreFile.isAbsolute) {
            rawStoreFile
        } else {
            signingFile.parentFile.resolve(rawStoreFile)
        }
        val signingStoreFile = verifiedExternalSigningFile(resolvedStoreFile)
        externalReleaseSigningConfigured = true
        signingConfigs {
            create("release") {
                storeFile = signingStoreFile
                storePassword = requiredSigningValue("storePassword")
                keyAlias = requiredSigningValue("keyAlias")
                keyPassword = requiredSigningValue("keyPassword")
            }
        }
        buildTypes.getByName("release").signingConfig = signingConfigs.getByName("release")
    }
    buildTypes {
        release {
            isMinifyEnabled = true
            // Production currently has no canonical Firebase client config.
            // Keep release constants explicitly empty even if a developer has
            // local debug values; the signed manifest records this state.
            buildConfigField("String", "FIREBASE_APPLICATION_ID", "\"\"")
            buildConfigField("String", "FIREBASE_API_KEY", "\"\"")
            buildConfigField("String", "FIREBASE_PROJECT_ID", "\"\"")
            buildConfigField("String", "FIREBASE_SENDER_ID", "\"\"")
            manifestPlaceholders["davidPiReleaseSourceSha256"] =
                releaseSourceSha256.orNull ?: "missing-release-source-sha256"
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    ksp { arg("room.schemaLocation", "$projectDir/schemas") }
    packaging.resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
}

val validateDavidPiReleaseSourceSha256 = tasks.register("validateDavidPiReleaseSourceSha256") {
    inputs.property("releaseSourceSha256", releaseSourceSha256.orElse(""))
    doLast {
        if (rootProject.file("local.properties").exists()) {
            throw GradleException(
                "Release builds forbid clients/android/local.properties; " +
                    "machine-specific Firebase values are not an attested release input"
            )
        }
        val value = releaseSourceSha256.orNull ?: ""
        if (!sha256Pattern.matches(value)) {
            throw GradleException(
                "Release builds require DAVID_PI_ANDROID_SOURCE_SHA256 from " +
                    "scripts/android_release.py source-hash"
            )
        }
        if (!externalReleaseSigningConfigured) {
            throw GradleException(
                "Release builds require regular signing files outside the repository"
            )
        }
    }
}

tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    dependsOn(validateDavidPiReleaseSourceSha256)
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.01.01"))
    implementation("androidx.activity:activity-compose:1.10.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.media:media:1.7.0")
    // 1.9+ requires compileSdk 36; 1.8 is the newest line compatible with
    // this project's AGP 8.7 / compileSdk 35 toolchain.
    implementation("androidx.media3:media3-exoplayer:1.8.0")
    implementation("androidx.media3:media3-session:1.8.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("androidx.room:room-testing:2.6.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    androidTestImplementation(platform("androidx.compose:compose-bom:2025.01.01"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
}
