<#
Find ssh/scp invocations in .ps1 files with the PowerShell AST (tests/test_remote_helper.py).
Strings, line comments and inline block comments are handled by the parser, not by regex.
A hit is:
  1. a CommandAst whose command is ssh, ssh.exe, scp or scp.exe (bare, quoted, full path, or an expandable
     string such as "$env:WINDIR\...\ssh.exe" after & or .);
  2. a CommandAst whose resolved command (aliases resolved: iex, start, saps, ...) is a launcher - cmd, cmd.exe,
     Invoke-Expression, Start-Process, powershell, pwsh - and any string in it (constant or expandable) names ssh/scp;
  3. a [Diagnostics.Process]::Start(...) call whose arguments contain a string naming ssh/scp.
Plain text that only mentions ssh (Write-Host "ssh tunnel", comments) is not a hit.
A hit is allowed when its first line has a comment '# remote-ok: <reason>' (reason at least 5 characters).
Writes a JSON array of {file, line, text, allowed, reason, why} to -Out (UTF-8).
#>
param([Parameter(Mandatory)][string]$Out, [Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$Path)
$ErrorActionPreference = "Stop"
$names = @("ssh", "ssh.exe", "scp", "scp.exe")
$launchers = @("cmd", "cmd.exe", "invoke-expression", "start-process", "powershell", "powershell.exe", "pwsh", "pwsh.exe")
$mention = '(?i)(^|[^\w$.-])(ssh|scp)(\.exe)?(?![\w-])'

function Get-StrValue($e) {
    if ($e -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return $e.Value }
    if ($e -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { return $e.Value }
    return $null
}
function Test-ExeName($e) {
    $v = Get-StrValue $e
    if ($null -eq $v) { return $false }
    $leaf = ($v -split '[\\/]')[-1].ToLowerInvariant()
    return $names -contains $leaf
}
function Resolve-Name([string]$n) {
    if (-not $n) { return "" }
    $a = Get-Alias -Name $n -ErrorAction SilentlyContinue
    if ($a -and $a.Definition) { return "$($a.Definition)".ToLowerInvariant() }
    return $n.ToLowerInvariant()
}
function Test-Strings($ast) {
    $strs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
                                     $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true)
    return @($strs | Where-Object { (Get-StrValue $_) -match $mention }).Count -gt 0
}

$hits = foreach ($p in $Path) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors)
    if ($errors) { throw "parse error in ${p}: $($errors[0].Message) (line $($errors[0].Extent.StartLineNumber))" }
    $found = @()
    foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $first = $c.CommandElements[0]
        if (Test-ExeName $first) { $found += [pscustomobject]@{ node = $c; why = "command" }; continue }
        $name = Resolve-Name ($(if ($c.GetCommandName()) { [IO.Path]::GetFileName($c.GetCommandName()) } else { "" }))
        if ($launchers -contains $name -and (Test-Strings $c)) { $found += [pscustomobject]@{ node = $c; why = "launcher $name" } }
    }
    foreach ($m in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        $type = if ($m.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { $m.Expression.TypeName.FullName } else { "" }
        if ($m.Static -and $type -match '(?i)(^|\.)Diagnostics\.Process$' -and "$($m.Member)" -eq "Start" -and
            @($m.Arguments | Where-Object { Test-Strings $_ }).Count) {
            $found += [pscustomobject]@{ node = $m; why = "Process.Start" }
        }
    }
    foreach ($h in $found) {
        $line = $h.node.Extent.StartLineNumber
        $mark = $tokens | Where-Object {
            $_.Kind -eq "Comment" -and $_.Extent.StartLineNumber -eq $line -and $_.Text -match '^#\s*remote-ok:\s*(\S.*)$'
        } | Select-Object -First 1
        $reason = if ($mark) { ([regex]::Match($mark.Text, '^#\s*remote-ok:\s*(\S.*)$')).Groups[1].Value.Trim() } else { "" }
        [pscustomobject]@{ file = $p; line = $line; text = $h.node.Extent.Text; allowed = ($reason.Length -ge 5); reason = $reason; why = $h.why }
    }
}
[IO.File]::WriteAllText($Out, (ConvertTo-Json -InputObject @($hits) -Depth 3), [Text.UTF8Encoding]::new($false))
