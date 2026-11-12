plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 后端代码不分叉：构建时把仓库根目录的 app/、prompts/、content/ 原样拷进来，和电脑版是同一份。
// 默认取上一级目录（干净的检出）。工作区里有没提交的改动时，用 -PbackendRoot=<一份干净的导出> 指到别处。
val repoRoot: File = (findProperty("backendRoot") as String?)?.let { file(it) } ?: rootProject.projectDir.parentFile
val backendSrc = layout.buildDirectory.dir("backend-src")
val syncBackend by tasks.registering(Sync::class) {
    from(repoRoot) {
        include("app/**", "prompts/mind/**", "content/**", "run.py", ".env.example")
        exclude("**/__pycache__/**", "app/dafeilong/**")
        rename("\\.env\\.example", "env.example")      // 点开头的文件不一定会被打进去
    }
    into(backendSrc)
}

android {
    namespace = "ai.meanwhile.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.meanwhile.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.0.1-m0"
        ndk {
            // Python 3.12 起只有 64 位。x86_64 是给模拟器用的。
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        // 构建机上要有同一个小版本的 Python。Chaquopy 自己会找：Windows 上 `py -3.12`，Linux / macOS 上 `python3.12`。
        pip {
            // pydantic-core 是 Rust 写的，Chaquopy 自己的包库里没有；Flet 的包库里有同样标签的安卓版（2.46.4 ↔ pydantic 2.13.4）。
            options("--extra-index-url", "https://pypi.flet.dev")
            install("pydantic==2.13.4")
            install("pydantic-core==2.46.4")
            install("fastapi")
            install("uvicorn")          // 不要 uvicorn[standard]：那几个加速包是原生的，手机上没有
            install("requests")
            install("httpx")
            install("python-dotenv")
            // Pillow 必须用 Chaquopy 自己编的那版：Flet 包库里版本更高的那几个把依赖的 libtiff 等放在 Chaquopy 找不到的地方，
            // 装上去一导入就报 library "libtiff.so" not found。11.0.0 只有 Chaquopy 的包库有，写死它。
            install("Pillow==11.0.0")
            install("tzdata")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(backendSrc)
        }
    }
}

// Chaquopy 合并 Python 源码之前，先把后端拷好
tasks.configureEach {
    if (name.startsWith("merge") && name.endsWith("PythonSources")) {
        dependsOn(syncBackend)
    }
}
