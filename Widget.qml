import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// omawake bar widget (unofficial community plugin).
//
// The daemon writes $XDG_RUNTIME_DIR/omawake/state.json on every state
// change; this widget watches that file and runs the plugin's own
// `bin/omawake` CLI for every action. Left click pauses/resumes (or opens
// setup when it is not finished), right click opens the panel.
Panel {
  id: root
  moduleName: "io.github.rob-d3v.omawake"
  ipcTarget: "io.github.rob-d3v.omawake"

  readonly property string cli: decodeURIComponent(String(Qt.resolvedUrl("bin/omawake")).replace(/^file:\/\//, ""))
  readonly property string stateFile: Quickshell.env("XDG_RUNTIME_DIR") + "/omawake/state.json"
  readonly property string createUrl: "https://aniamodels.shop/create-wakeword"

  property var live: ({})
  property var info: ({})
  readonly property bool setupOk: info.setup_ok === true
  readonly property bool running: info.running === true || (live.pid !== undefined && live.state !== "stopped" && fresh)
  property bool fresh: false
  readonly property string st: !setupOk && info.setup_ok !== undefined ? "setup"
    : (running ? String(live.state || info.state || "idle") : "stopped")
  readonly property bool hot: ["listening", "conversation", "transcribing", "typing"].indexOf(st) !== -1
  readonly property var wakeWords: info.wake_words || []
  // Wake words from config.toml with their actions (live from the daemon when it runs).
  readonly property var words: (running && live.words) ? live.words : (info.words || [])
  readonly property bool legacy: String(live.config_source || info.config_source || "legacy") === "legacy"
  readonly property string unsafeReason: String(live.unsafe_reason || info.unsafe_reason || "")
  readonly property var configErrors: live.config_errors || info.config_errors || []

  function actionIcon(a) {
    if (a === "dictate") return String.fromCodePoint(0xF030C)       // keyboard
    if (a === "command") return String.fromCodePoint(0xF018D)       // console
    if (a === "agent-prompt") return String.fromCodePoint(0xF0361)  // message
    return String.fromCodePoint(0xF06A9)                            // robot
  }

  function glyph() {
    if (st === "paused" || st === "stopped") return String.fromCodePoint(0xF036D)      // mic off
    if (st === "error") return String.fromCodePoint(0xF036D)
    if (st === "setup") return String.fromCodePoint(0xF036F)                           // mic + gear (md-microphone_settings)
    if (st === "conversation") return String.fromCodePoint(0xF05CB)                    // voice
    return String.fromCodePoint(0xF036C)                                               // mic
  }

  function label() {
    switch (st) {
    case "idle": return "Listening for “" + (live.phrase || "wake word") + "”"
    case "listening": return "Listening…"
    case "transcribing": return "Transcribing…"
    case "typing": return "Sending to agent…"
    case "running": return "Running command…"
    case "conversation": return "Conversation"
    case "paused": return "Paused"
    case "setup": return "Setup needed"
    case "error": return "Error"
    case "stopped": return "Not running"
    }
    return st
  }

  function run(args) {
    actionProc.command = [root.cli].concat(args)
    actionProc.running = true
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  function primary() {
    if (!setupOk) Quickshell.execDetached([root.cli, "open-setup"])
    else if (!running) Quickshell.execDetached(["systemctl", "--user", "start", "omawake.service"])
    else run(["toggle"])
  }

  onOpenedChanged: if (opened) refresh()

  FileView {
    id: stateView
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        root.live = JSON.parse(text())
        root.fresh = true
      } catch (e) {}
    }
    onLoadFailed: { root.live = ({}); root.fresh = false }
  }

  Process {
    id: statusProc
    command: [root.cli, "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.info = JSON.parse(text) } catch (e) {}
      }
    }
  }

  Process {
    id: actionProc
    onExited: root.refresh()
  }

  Timer {
    interval: root.opened ? 3000 : 15000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.glyph()
    active: root.hot
    dimmed: root.st === "paused" || root.st === "stopped"
    foreground: root.st === "error" ? Commons.Color.urgent : (root.bar ? root.bar.barForeground : Commons.Color.foreground)
    tooltipText: "omawake: " + root.label() + (root.setupOk ? "" : " (click to finish setup)")
    onPressed: function(b) {
      if (b === Qt.RightButton || b === Qt.MiddleButton) root.toggle()
      else root.primary()
    }

    SequentialAnimation on opacity {
      running: root.st === "listening" || root.st === "conversation"
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { from: 1.0; to: 0.45; duration: 700; easing.type: Easing.InOutSine }
      NumberAnimation { from: 0.45; to: 1.0; duration: 700; easing.type: Easing.InOutSine }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroText.implicitHeight)

          Text {
            id: heroIcon
            text: root.glyph()
            color: root.hot ? Commons.Color.accent : root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroText
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              text: "omawake"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              text: root.label().toUpperCase()
              color: root.st === "error" ? Commons.Color.urgent : Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        Text {
          visible: text !== "" && (root.st === "error" || root.st === "setup")
          width: parent.width
          wrapMode: Text.Wrap
          text: String(root.live.message || "")
          color: root.bar.foreground
          opacity: 0.7
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Button {
          visible: !root.setupOk
          width: parent.width
          iconText: String.fromCodePoint(0xF036F)
          text: "Finish setup"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          active: true
          onClicked: { Quickshell.execDetached([root.cli, "open-setup"]); root.close() }
        }

        Text {
          visible: text !== "" && root.running && root.st === "idle"
          width: parent.width
          wrapMode: Text.Wrap
          maximumLineCount: 2
          elide: Text.ElideRight
          text: String(root.live.last_result || "")
          color: root.bar.foreground
          opacity: 0.6
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- wake words + actions (config.toml) ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          PanelSectionHeader { text: "WAKE WORDS"; foreground: root.bar.foreground; fontFamily: root.bar.fontFamily }

          Repeater {
            model: root.words
            Item {
              id: wordRow
              required property var modelData
              width: parent.width
              implicitHeight: Math.max(wordText.implicitHeight, wordSwitch.implicitHeight)
              opacity: modelData.installed && !modelData.ania_player ? 1 : 0.5

              Text {
                id: wordIcon
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: root.actionIcon(wordRow.modelData.action)
                color: wordRow.modelData.active && root.running ? Commons.Color.accent : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
              }
              Column {
                id: wordText
                anchors.left: wordIcon.right
                anchors.leftMargin: Style.space(10)
                anchors.right: wordSwitch.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                Text {
                  width: parent.width
                  elide: Text.ElideRight
                  text: "“" + wordRow.modelData.phrase + "”"
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
                Text {
                  width: parent.width
                  elide: Text.ElideRight
                  text: wordRow.modelData.ania_player ? "skipped: Ania Player uses this word"
                        : wordRow.modelData.installed ? wordRow.modelData.describe : "not installed: omawake models"
                  color: root.bar.foreground
                  opacity: 0.6
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              ToggleSwitch {
                id: wordSwitch
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.bar.foreground
                checked: wordRow.modelData.enabled === true
                onToggled: root.run([checked ? "disable" : "enable", wordRow.modelData.model])
              }
            }
          }

          Text {
            visible: root.unsafeReason !== "" || root.configErrors.length > 0
            width: parent.width
            wrapMode: Text.Wrap
            text: root.unsafeReason !== "" ? "Commands disabled: " + root.unsafeReason
                                           : "config.toml: " + root.configErrors.join("; ")
            color: Commons.Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Row {
            id: cfgRow
            width: parent.width
            spacing: Style.space(6)
            Button {
              width: (cfgRow.width - cfgRow.spacing) / 2
              leftAlign: true
              iconText: String.fromCodePoint(0xF03EB)
              text: "Edit config…"
              fontSize: Style.font.bodySmall
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: { Quickshell.execDetached([root.cli, "config", "edit"]); root.close() }
            }
            Button {
              width: (cfgRow.width - cfgRow.spacing) / 2
              leftAlign: true
              iconText: String.fromCodePoint(0xF0450)
              text: "Reload"
              fontSize: Style.font.bodySmall
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.run(["reload"])
            }
          }
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- mode (only without config.toml) ----------
        Column {
          visible: root.legacy
          width: parent.width
          spacing: Style.space(8)
          PanelSectionHeader { text: "MODE"; foreground: root.bar.foreground; fontFamily: root.bar.fontFamily }
          Row {
            id: modeRow
            width: parent.width
            spacing: Style.space(6)
            Repeater {
              model: [{ id: "ask", label: "Ask agent", icon: String.fromCodePoint(0xF06A9) }, { id: "dictate", label: "Dictate", icon: String.fromCodePoint(0xF030C) }]
              Button {
                required property var modelData
                width: (modeRow.width - modeRow.spacing) / 2
                iconText: modelData.icon
                text: modelData.label
                fontSize: Style.font.bodySmall
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                bordered: true
                active: (root.live.mode || root.info.mode) === modelData.id
                onClicked: root.run(["mode", modelData.id])
              }
            }
          }
        }

        // ---------- sensitivity ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          PanelSectionHeader { text: "SENSITIVITY"; foreground: root.bar.foreground; fontFamily: root.bar.fontFamily }
          Row {
            id: sensRow
            width: parent.width
            spacing: Style.space(6)
            Repeater {
              model: ["low", "normal", "high"]
              Button {
                required property var modelData
                width: (sensRow.width - sensRow.spacing * 2) / 3
                text: modelData.charAt(0).toUpperCase() + modelData.slice(1)
                fontSize: Style.font.bodySmall
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                bordered: true
                active: (root.live.sensitivity || root.info.sensitivity) === modelData
                onClicked: root.run(["sensitivity", modelData])
              }
            }
          }
        }

        // ---------- wake word (only without config.toml) ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          PanelSectionHeader { visible: root.legacy; text: "WAKE WORD"; foreground: root.bar.foreground; fontFamily: root.bar.fontFamily }
          Repeater {
            model: root.legacy ? root.wakeWords : []
            Button {
              required property var modelData
              width: parent.width
              leftAlign: true
              enabled: modelData.status === "installed"
              opacity: enabled ? 1 : 0.5
              text: modelData.phrase + (modelData.status === "installed" ? "" : "  (" + modelData.status + ")")
              fontSize: Style.font.bodySmall
              foreground: root.bar.foreground
              fontFamily: root.bar.fontFamily
              active: (root.live.wake_word || root.info.wake_word) === modelData.id
              onClicked: root.run(["wake", modelData.id])
            }
          }
          Button {
            width: parent.width
            leftAlign: true
            iconText: String.fromCodePoint(0xF0415)
            text: "Create your own wake word…"
            fontSize: Style.font.bodySmall
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            onClicked: { Quickshell.execDetached(["xdg-open", root.createUrl]); root.close() }
          }
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- conversation + actions ----------
        Item {
          width: parent.width
          implicitHeight: convSwitch.implicitHeight
          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Keep listening after a reply"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          ToggleSwitch {
            id: convSwitch
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            foreground: root.bar.foreground
            checked: root.live.conversation !== undefined ? root.live.conversation === true : root.info.conversation === true
            onToggled: root.run(["conversation", checked ? "off" : "on"])
          }
        }

        Row {
          id: actions
          width: parent.width
          spacing: Style.space(6)
          Button {
            width: (actions.width - actions.spacing) / 2
            iconText: root.st === "paused" ? String.fromCodePoint(0xF040A) : String.fromCodePoint(0xF03E4)
            text: root.running ? (root.st === "paused" ? "Resume" : "Pause") : "Start"
            enabled: root.setupOk
            fontSize: Style.font.bodySmall
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            bordered: true
            onClicked: root.primary()
          }
          Button {
            width: (actions.width - actions.spacing) / 2
            iconText: String.fromCodePoint(0xF0219)
            text: "Open logs"
            fontSize: Style.font.bodySmall
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            bordered: true
            onClicked: { Quickshell.execDetached([root.cli, "logs", "--open"]); root.close() }
          }
        }
      }
    }
  }
}
