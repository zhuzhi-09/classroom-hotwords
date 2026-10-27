# 用 Windows 自带的中文语音（Huihui）把 lesson.txt 合成为 16kHz 单声道测试音频
Add-Type -AssemblyName System.Speech
$dir = Join-Path $PSScriptRoot "testdata"
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice("Microsoft Huihui Desktop")
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile((Join-Path $dir "lesson_clean.wav"), $fmt)
$p = New-Object System.Speech.Synthesis.PromptBuilder([System.Globalization.CultureInfo]::GetCultureInfo("zh-CN"))
foreach ($l in Get-Content -Encoding UTF8 (Join-Path $dir "lesson.txt")) {
    $p.AppendText($l)
    $p.AppendBreak([TimeSpan]::FromMilliseconds(1200))
}
$s.Speak($p)
$s.Dispose()
