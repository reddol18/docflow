<#
Find ssh/scp invocations in .ps1 files with the PowerShell AST (tests/test_remote_helper.py).
Strings, line comments and inline block comments are handled by the parser, not by regex.
Hit = CommandAst whose command name (also via & or a full path) is ssh, ssh.exe, scp or scp.exe,
or Start-Process with such a FilePath. A hit is allowed when its first line has a comment '# remote-ok: <reason>'.
Writes a JSON array of {file, line, text, allowed, reason} to -Out (UTF-8).
#>
param([Parameter(Mandatory)][string]$Out, [Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$Path)
$ErrorActionPreference = "Stop"
$names = @("ssh", "ssh.exe", "scp", "scp.exe")
function Test-Name($e) {
    if ($e -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $false }
    return $names -contains [IO.Path]::GetFileName($e.Value).ToLowerInvariant()
}
$hits = foreach ($p in $Path) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors)
    if ($errors) { throw "parse error in ${p}: $($errors[0].Message) (line $($errors[0].Extent.StartLineNumber))" }
    $cmds = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
    foreach ($c in $cmds) {
        $first = $c.CommandElements[0]
        $hit = Test-Name $first
        if (-not $hit -and $c.GetCommandName() -eq "Start-Process") {
            $hit = @($c.CommandElements | Select-Object -Skip 1 | Where-Object { Test-Name $_ }).Count -gt 0
        }
        if (-not $hit) { continue }
        $line = $c.Extent.StartLineNumber
        $mark = $tokens | Where-Object {
            $_.Kind -eq "Comment" -and $_.Extent.StartLineNumber -eq $line -and $_.Text -match '^#\s*remote-ok:\s*(\S.*)$'
        } | Select-Object -First 1
        $reason = if ($mark) { ([regex]::Match($mark.Text, '^#\s*remote-ok:\s*(\S.*)$')).Groups[1].Value.Trim() } else { "" }
        [pscustomobject]@{ file = $p; line = $line; text = $c.Extent.Text; allowed = ($reason.Length -ge 5); reason = $reason }
    }
}
[IO.File]::WriteAllText($Out, (ConvertTo-Json -InputObject @($hits) -Depth 3), [Text.UTF8Encoding]::new($false))
