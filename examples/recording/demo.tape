Output docs/assets/demo.gif

Require python
Set Shell "bash"
Set FontSize 17
Set Padding 30
Set Width 1000
Set Height 600
Set WindowBar ""
Set Theme "Dracula"
Set Framerate 10
Set TypingSpeed 40ms
Env MEMORYOS_DEMO_MOCK "1"
Env PROMPT_COMMAND "PS1='$ '"

Type "python examples/recording/1_write.py"
Enter
Sleep 3s

Type "python examples/recording/2_conflict.py"
Enter
Sleep 3s

Type "python examples/recording/3_ask_agent.py"
Enter
Sleep 3s

Type "python examples/recording/4_operator_resolve.py"
Enter
Sleep 3s

Type "python examples/recording/5_history.py"
Enter
Sleep 3.6s
