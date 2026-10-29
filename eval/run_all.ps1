# #88 인·허가 7개 철 680쪽을 모두 올리고 평가용(auto, 사람 검토 없이)으로 실행을 걸어 둔다.
# docflow는 철을 한 번에 하나씩 처리하므로 순서대로 대기열에 쌓인다. 원본 폴더는 읽기만 한다.
$ErrorActionPreference = 'Stop'
Set-Location E:\claude-dev\base\records_poc
$py = '.venv\Scripts\python.exe'
# 원본 폴더는 client.py가 정한다 (이 파일은 BOM 없는 UTF-8이라 PowerShell 5.1이 한글 경로를 깨뜨린다)
$vols = '5350109-1997-0002', '5350109-1994-0001', '5350109-1997-0001', '5350109-1999-0001',
        '5350109-1999-0002', '5350109-1999-0003', '5350109-2001-0001'
if ($args.Count) { $vols = $args }
foreach ($v in $vols) {
    & $py -I -X utf8 doc_platform\eval\client.py upload $v
    if ($LASTEXITCODE -ne 0) { Write-Output "upload failed: $v"; continue }
    & $py -I -X utf8 doc_platform\eval\client.py run $v auto
}
