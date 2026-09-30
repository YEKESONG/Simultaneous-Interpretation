// si_audio_tap.swift —— 用 Core Audio Taps（macOS 14.2+）内录系统正在播放的声音
//
// 输出：stdout 上连续写出单声道 float32 小端 PCM，默认 16 kHz（--sample-rate 可改）。
// 日志：stderr 上每行一个 JSON，例如 {"event":"started",...}，由 Python 端解析。
//
// 关键点：
// 1. CATapDescription(monoGlobalTapButExcludeProcesses: []) = 把所有进程的声音混成一路单声道；
// 2. muteBehavior = .unmuted：只“复制”一份声音，原声照常从耳机/扬声器播放；
// 3. tap 不能直接读，要挂到一个私有的聚合设备（aggregate device）上，再给聚合设备注册 IOProc 回调来取数据；
// 4. 聚合设备里只放 tap、不放任何真实设备，这样不会混进耳机麦克风之类的输入；
// 5. 默认输出设备变化（插拔耳机、连蓝牙）时以退出码 3 退出，由 Python 端立即重启，重新挂到新设备上；
// 6. 权限归属：macOS 把“系统录音”权限记在“负责进程”（启动它的应用，如终端、Claude）头上。
//    没权限时系统不报错，只给全零的静音。所以程序启动后先用 responsibility_spawnattrs_setdisclaim
//    重新启动自己、声明“我自己负责”，这样权限弹窗显示的是 si-audio-tap 本身，从哪里启动都一样。

import AVFoundation
import CoreAudio
import Foundation

let exitCodeOutputDeviceChanged: Int32 = 3

struct TapError: Error, CustomStringConvertible {
    let description: String
}

func check(_ status: OSStatus, _ what: String) throws {
    guard status == noErr else { throw TapError(description: "\(what)失败（OSStatus=\(status)）") }
}

func logEvent(_ event: String, _ fields: [String: Any] = [:]) {
    var object = fields
    object["event"] = event
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    FileHandle.standardError.write(data + Data("\n".utf8))
}

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
}

func defaultOutputDevice() -> AudioObjectID {
    var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device)
    return device
}

func deviceName(_ device: AudioObjectID) -> String {
    var addr = address(kAudioObjectPropertyName)
    var name: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &name) == noErr,
          let value = name?.takeRetainedValue() else { return "unknown" }
    return value as String
}

/// 把数据完整写进 stdout。读取端（Python）关闭管道时直接退出，进程退出后私有的 tap 和聚合设备会被系统回收。
func writeToStdout(_ pointer: UnsafeRawPointer, _ count: Int) {
    var offset = 0
    while offset < count {
        let written = write(STDOUT_FILENO, pointer + offset, count - offset)
        if written < 0 {
            if errno == EINTR { continue }
            exit(0)
        }
        offset += written
    }
}

final class SystemAudioTap {
    private let targetFormat: AVAudioFormat
    private let ioQueue = DispatchQueue(label: "si-audio-tap.io", qos: .userInteractive)
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var sourceFormat: AVAudioFormat?
    private var converter: AVAudioConverter?

    init(sampleRate: Double) {
        targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
    }

    func start() throws {
        // 1) 创建 tap：所有进程的声音混成单声道，不静音原声
        let description = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        description.name = "si-audio-tap"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(description, &tapID), "创建 process tap ")

        // 2) 读 tap 的原始格式（通常是 48 kHz float32），准备重采样到目标格式
        var streamDescription = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var formatAddress = address(kAudioTapPropertyFormat)
        try check(
            AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &size, &streamDescription), "读取 tap 音频格式")
        guard let format = AVAudioFormat(streamDescription: &streamDescription),
              let converter = AVAudioConverter(from: format, to: targetFormat)
        else { throw TapError(description: "不支持的 tap 音频格式：\(streamDescription)") }
        sourceFormat = format
        self.converter = converter

        // 3) 创建只包含这个 tap 的私有聚合设备
        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey: "si-audio-tap",
            kAudioAggregateDeviceUIDKey: "si-audio-tap-\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [] as [Any],
            kAudioAggregateDeviceTapListKey: [
                [kAudioSubTapUIDKey: description.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]
            ],
        ]
        try check(
            AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID), "创建聚合设备")

        // 4) 注册回调并启动：之后每约 10 ms 回调一次，拿到一小段声音
        try check(
            AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, ioQueue) { [weak self] _, input, _, _, _ in
                self?.process(input)
            }, "注册 IOProc ")
        try check(AudioDeviceStart(aggregateID, ioProcID), "启动聚合设备")

        logEvent("started", [
            "source_sample_rate": format.sampleRate,
            "source_channels": Int(format.channelCount),
            "sample_rate": targetFormat.sampleRate,
            "output_device": deviceName(defaultOutputDevice()),
        ])
    }

    func stop() {
        if let procID = ioProcID {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
            ioProcID = nil
        }
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    private func process(_ inputData: UnsafePointer<AudioBufferList>) {
        guard let sourceFormat, let converter,
              let input = AVAudioPCMBuffer(pcmFormat: sourceFormat, bufferListNoCopy: inputData, deallocator: nil),
              input.frameLength > 0
        else { return }
        let ratio = targetFormat.sampleRate / sourceFormat.sampleRate
        let capacity = AVAudioFrameCount((Double(input.frameLength) * ratio).rounded(.up)) + 16
        guard let output = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

        // 转换器内部保留滤波状态，所以每次只喂这一小段，连续调用也不会在拼接处产生杂音
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return input
        }
        guard status != .error, output.frameLength > 0, let samples = output.floatChannelData?[0] else { return }
        writeToStdout(samples, Int(output.frameLength) * MemoryLayout<Float>.size)
    }
}

// ---- 权限归属：重新启动自己，并声明由自己负责 ----

// libSystem 导出但没有公开头文件的函数（Qt Creator、LLDB 等工具也用它做同样的事）
@_silgen_name("responsibility_spawnattrs_setdisclaim")
func responsibility_spawnattrs_setdisclaim(_ attrs: UnsafeMutablePointer<posix_spawnattr_t?>, _ disclaim: Int32)
    -> Int32

let disclaimedEnv = "SI_AUDIO_TAP_DISCLAIMED"

/// 以“自己负责”的身份重新启动自己，父进程只负责转发信号、等待子进程并返回它的退出码。
/// 子进程继承 stdout/stderr，所以 Python 端读到的数据不受影响。
func relaunchAsResponsibleProcess() {
    guard getenv(disclaimedEnv) == nil, let executable = Bundle.main.executablePath else { return }
    setenv(disclaimedEnv, "1", 1)

    var attributes: posix_spawnattr_t?
    posix_spawnattr_init(&attributes)
    defer { posix_spawnattr_destroy(&attributes) }
    guard responsibility_spawnattrs_setdisclaim(&attributes, 1) == 0 else { return }

    let argv = CommandLine.arguments.map { strdup($0) } + [nil]
    defer { argv.forEach { free($0) } }
    var child: pid_t = 0
    guard posix_spawn(&child, executable, nil, &attributes, argv, environ) == 0 else { return }

    // 先记下子进程号再装信号处理；注意 kill(0, …) 会发给整个进程组，所以必须判断 > 0
    childPID = child
    for signalNumber in [SIGINT, SIGTERM, SIGHUP] {
        signal(signalNumber) { received in
            if childPID > 0 { kill(childPID, received) }
        }
    }
    var status: Int32 = 0
    while waitpid(child, &status, 0) < 0 && errno == EINTR {}
    let exited = (status & 0x7f) == 0
    exit(exited ? (status >> 8) & 0xff : 1)
}

var childPID: pid_t = 0

// ---- 入口 ----

relaunchAsResponsibleProcess()

var sampleRate = 16000.0
var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--sample-rate":
        guard let value = arguments.next(), let rate = Double(value), rate >= 8000, rate <= 48000 else {
            logEvent("error", ["message": "--sample-rate 需要 8000~48000 之间的数"])
            exit(2)
        }
        sampleRate = rate
    case "-h", "--help":
        print("用法：si-audio-tap [--sample-rate 16000]\n内录系统声音，stdout 输出单声道 float32 PCM，stderr 输出 JSON 日志。")
        exit(0)
    default:
        logEvent("error", ["message": "未知参数 \(argument)"])
        exit(2)
    }
}

let tap = SystemAudioTap(sampleRate: sampleRate)
let controlQueue = DispatchQueue(label: "si-audio-tap.control")

func shutdown(_ code: Int32, reason: String) -> Never {
    tap.stop()
    logEvent("stopped", ["reason": reason])
    exit(code)
}

do {
    try tap.start()
} catch {
    logEvent("error", ["message": "\(error)"])
    exit(1)
}

// 默认输出设备变了：退出并让 Python 端重启，重新挂到新设备上
var outputAddress = address(kAudioHardwarePropertyDefaultOutputDevice)
AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &outputAddress, controlQueue) { _, _ in
    shutdown(exitCodeOutputDeviceChanged, reason: "output_device_changed")
}

// Ctrl+C / kill：先清理再退出；管道断开由 writeToStdout 处理
signal(SIGPIPE, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for signalNumber in [SIGINT, SIGTERM] {
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: controlQueue)
    source.setEventHandler { shutdown(0, reason: "signal") }
    source.resume()
    signalSources.append(source)
}

dispatchMain()
