Option Explicit

' =============================================================================
' CTC_Macro.bas
' Resource Forecast - CTC Template Macro
' Excel 365
' =============================================================================

' ---------------------------------------------------------------------------
' CONFIGURATION
' ---------------------------------------------------------------------------
Private Const API_BASE   As String = "http://localhost:5000"
Private Const PASSWORD   As String = "Cyberdyne"

' Header cell addresses
Private Const CELL_START_DATE      As String = "E16"
Private Const CELL_PROJECT_NUMBER  As String = "C5"
Private Const CELL_TASK_ORDER      As String = "C6"
Private Const CELL_ORGANISATION    As String = "C7"
Private Const CELL_CUSTOMER        As String = "C8"
Private Const CELL_PROJECT_NAME    As String = "C9"
Private Const CELL_TASK_NAME       As String = "C10"
Private Const CELL_DIRECTOR        As String = "C11"
Private Const CELL_MANAGER         As String = "C12"
Private Const CELL_LAST_UPDATED_BY As String = "C13"
Private Const CELL_FILE_PATH       As String = "A1" ' (hidden)
Private Const CELL_IS_SAVED        As String = "A2" ' (hidden) "1" once first save has completed

' Staff grid layout
Private Const WORKING_DAYS_ROW As Long = 15
Private Const HEADER_ROW       As Long = 16
Private Const FIRST_DATA_ROW   As Long = 17
Private Const LAST_DATA_ROW    As Long = 56
Private Const COL_HORIZON_ID   As Long = 1   ' A (hidden)
Private Const COL_NAME         As Long = 2   ' B
Private Const COL_JOB_TITLE    As Long = 3   ' C
Private Const COL_JOB_FUNC     As Long = 4   ' D
Private Const ALLOC_FIRST_COL  As Long = 5   ' E - first month
Private Const NUM_MONTHS       As Long = 36

' Colours
Private Const COLOR_HEADER_NORMAL    As Long = RGB(151, 193, 231)
Private Const COLOR_HEADER_CURRENT   As Long = RGB(23, 55, 94)
Private Const COLOR_DATA_NORMAL      As Long = RGB(255, 255, 255)
Private Const COLOR_DATA_CURRENT     As Long = RGB(220, 230, 241)
Private Const COLOR_START_UNSAVED    As Long = RGB(255, 192, 0)   ' orange


' =============================================================================
' ON OPEN
' Called from ThisWorkbook.Workbook_Open
' =============================================================================

Public Sub OnOpen()

    PopulateStaffDropdown
    HighlightCurrentMonth
    EnforceStartDateLock
    Application.StatusBar = False

End Sub


' Defensive check — re-applies the start date lock on every open if the
' file has already been saved once, in case protection was somehow lost
' (e.g. file edited outside Excel, or protection reset manually).
Private Sub EnforceStartDateLock()

    If GetCell(CELL_IS_SAVED) <> "1" Then Exit Sub

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    If Not ws.Range(CELL_START_DATE).Locked Then
        ws.Unprotect Password:=PASSWORD
        ws.Range(CELL_START_DATE).Locked = True
        ws.Protect Password:=PASSWORD, Contents:=True, _
            DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True
    End If

End Sub


' =============================================================================
' BEFORE SAVE
' Called from ThisWorkbook.Workbook_BeforeSave
' =============================================================================

Public Sub OnBeforeSave(Cancel As Boolean)

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ' --- Check file extension --------------------------------------------
    ' Must be .xlsm or the macro itself will be stripped out on save,
    ' silently breaking the push to the server with no error shown.
    Dim ext As String
    ext = LCase(Right(ThisWorkbook.FullName, 5))
    If ext <> ".xlsm" Then
        MsgBox "This file must be saved as an Excel Macro-Enabled Workbook (.xlsm)." & vbCrLf & _
               "Any other format (such as .xlsx) will remove the code that sends" & vbCrLf & _
               "your data to the resource forecast server." & vbCrLf & vbCrLf & _
               "Please choose .xlsm from the 'Save as type' list and try again.", _
               vbCritical, "Wrong file format"
        Cancel = True
        Exit Sub
    End If

    ' --- Validate CTCStartDate -------------------------------------------
    Dim startDate As Variant
    startDate = ws.Range(CELL_START_DATE).Value
    If IsEmpty(startDate) Or startDate = "" Then
        MsgBox "Please set the CTC Start Date before saving." & vbCrLf & _
               "Enter a date in the orange cell.", _
               vbExclamation, "Save blocked"
        ws.Range(CELL_START_DATE).Select
        Cancel = True
        Exit Sub
    End If

    ' --- First-save warning + lock-down -----------------------------------
    ' The start date drives every month column via formula. Once allocations
    ' exist against those months, changing the start date would silently
    ' shift every figure into the wrong month. So it can only be set freely
    ' up to and including the first save; after that it is locked.
    Dim isSaved As Boolean
    isSaved = (GetCell(CELL_IS_SAVED) = "1")

    If Not isSaved Then
        Dim response As VbMsgBoxResult
        response = MsgBox("The CTC Start Date cannot be changed once this file has been saved." & vbCrLf & vbCrLf & _
               "Current start date: " & Format(startDate, "mmm-yy") & vbCrLf & vbCrLf & _
               "Continue saving with this start date?", _
               vbExclamation + vbYesNo, "Start date will be locked")
        If response = vbNo Then
            Cancel = True
            ws.Range(CELL_START_DATE).Select
            Exit Sub
        End If
    End If

    ' --- Validate Project Number ----------------------------------------
    Dim projNum As String
    projNum = Trim(GetCell(CELL_PROJECT_NUMBER))
    If projNum = "" Then
        MsgBox "Please enter a Project Number before saving.", _
               vbExclamation, "Save blocked"
        ws.Range(CELL_PROJECT_NUMBER).Select
        Cancel = True
        Exit Sub
    End If

    ' --- Warn about placeholder (non-blocking) --------------------------
    If IsPlaceholder(projNum) Then
        MsgBox "Reminder: project number (" & projNum & ") looks like a placeholder." & vbCrLf & _
               "Update it with the Horizon number when available." & vbCrLf & _
               "The file will save normally.", vbInformation, "Project number"
    End If

    ' --- Look up Horizon record if not yet populated --------------------
    If GetCell(CELL_PROJECT_NAME) = "No Horizon Record Found" _
       Or GetCell(CELL_PROJECT_NAME) = "" Then
        Dim taskOrder As String
        taskOrder = Trim(GetCell(CELL_TASK_ORDER))
        If projNum <> "" And taskOrder <> "" And Not IsPlaceholder(projNum) Then
            LookupProject projNum, taskOrder
        End If
    End If

    ' --- Write macro-owned fields ---------------------------------------
    ws.Unprotect PASSWORD:=PASSWORD
    ws.Range(CELL_LAST_UPDATED_BY).Value = Application.UserName
    ws.Range(CELL_FILE_PATH).Value = ThisWorkbook.FullName
    ws.Protect PASSWORD:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

    ' --- Push to server -------------------------------------------------
    If Not PushToAPI() Then
        MsgBox "Could not connect to the resource forecast server." & vbCrLf & _
               "The file has been saved locally." & vbCrLf & _
               "Data will sync on the next successful save.", _
               vbExclamation, "Server not reachable"
    End If

    ' --- Lock down start date after first save ---------------------------
    If Not isSaved Then
        LockStartDate
    End If

End Sub


' =============================================================================
' NAME SELECTED
' Called from ThisWorkbook.Workbook_SheetChange
' Fires when a name is selected in the staff grid (column B)
' =============================================================================

Public Sub OnNameSelected(ws As Worksheet, changedCell As Range)

    If changedCell.Column <> COL_NAME Then Exit Sub
    If changedCell.Row < FIRST_DATA_ROW Or changedCell.Row > LAST_DATA_ROW Then Exit Sub

    Dim selectedName As String
    selectedName = Trim(changedCell.Value)

    ws.Unprotect PASSWORD:=PASSWORD

    If selectedName = "" Then
        ws.Cells(changedCell.Row, COL_HORIZON_ID).Value = ""
        ws.Cells(changedCell.Row, COL_JOB_TITLE).Value = ""
        ws.Cells(changedCell.Row, COL_JOB_FUNC).Value = ""
        GoTo Reprotect
    End If

    ' Look up in hidden _StaffData sheet
    Dim wsData As Worksheet
    On Error Resume Next
    Set wsData = ThisWorkbook.Sheets("_StaffData")
    On Error GoTo 0

    If wsData Is Nothing Then
        MsgBox "Staff data not loaded. Please close and reopen the file.", vbExclamation
        GoTo Reprotect
    End If

    Dim lastRow As Long
    lastRow = wsData.Cells(wsData.Rows.Count, 2).End(xlUp).Row

    Dim i As Long
    For i = 2 To lastRow
        If wsData.Cells(i, 2).Value = selectedName Then
            ws.Cells(changedCell.Row, COL_HORIZON_ID).Value = wsData.Cells(i, 1).Value
            ws.Cells(changedCell.Row, COL_JOB_TITLE).Value = wsData.Cells(i, 3).Value
            ws.Cells(changedCell.Row, COL_JOB_FUNC).Value = wsData.Cells(i, 4).Value
            Exit For
        End If
    Next i

Reprotect:
    ws.Protect PASSWORD:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


' =============================================================================
' PROJECT LOOKUP
' Calls API to get project details by number and task order
' =============================================================================

Private Sub LookupProject(projNum As String, taskOrder As String)

    Dim url As String
    url = API_BASE & "/api/project?project_number=" & URLEncode(projNum) & _
          "&task_order_number=" & URLEncode(taskOrder)

    Dim data As String
    data = HttpGet(url)
    If data = "" Then Exit Sub

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect PASSWORD:=PASSWORD

    Dim v As String
    v = ParseJsonField(data, "project_name")
    If v <> "" Then ws.Range(CELL_PROJECT_NAME).Value = v

    v = ParseJsonField(data, "task_name")
    If v <> "" Then ws.Range(CELL_TASK_NAME).Value = v

    v = ParseJsonField(data, "project_organisation")
    If v <> "" Then ws.Range(CELL_ORGANISATION).Value = v

    v = ParseJsonField(data, "project_customer")
    If v <> "" Then ws.Range(CELL_CUSTOMER).Value = v

    v = ParseJsonField(data, "project_director")
    If v <> "" Then ws.Range(CELL_DIRECTOR).Value = v

    v = ParseJsonField(data, "project_manager")
    If v <> "" Then ws.Range(CELL_MANAGER).Value = v

    ws.Protect PASSWORD:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


' =============================================================================
' STAFF DROPDOWN
' =============================================================================

Private Sub PopulateStaffDropdown()

    Dim data As String
    data = HttpGet(API_BASE & "/api/staff")
    If data = "" Then Exit Sub

    ' Write to hidden _StaffData sheet
    Dim wsData As Worksheet
    On Error Resume Next
    Set wsData = ThisWorkbook.Sheets("_StaffData")
    On Error GoTo 0

    If wsData Is Nothing Then
        Set wsData = ThisWorkbook.Sheets.Add( _
            After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        wsData.Name = "_StaffData"
        
    Else
        wsData.Cells.Clear
    End If

    ' Headers in row 1
    wsData.Cells(1, 1).Value = "Horizon ID"
    wsData.Cells(1, 2).Value = "Name"
    wsData.Cells(1, 3).Value = "Job Title"
    wsData.Cells(1, 4).Value = "Job Function"

    Dim rowCount As Long
    rowCount = WriteStaffData(wsData, data)
    If rowCount = 0 Then Exit Sub
    wsData.Visible = xlSheetVeryHidden

    ' Apply dropdown to name column in staff grid
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect PASSWORD:=PASSWORD

    With ws.Range(ws.Cells(FIRST_DATA_ROW, COL_NAME), _
                  ws.Cells(LAST_DATA_ROW, COL_NAME)).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="=_StaffData!$B$2:$B$" & (rowCount + 1)
        .ShowError = False
    End With

    ws.Protect PASSWORD:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


Private Sub LockStartDate()

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:=PASSWORD

    ' Record that the file has now been saved once — start date is
    ' permanently locked from this point on.
    ws.Range(CELL_IS_SAVED).Value = "1"
    ws.Range(CELL_START_DATE).Locked = True

    ws.Protect Password:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

    ' Re-run so the first month column resolves to its proper
    ' normal/current colour instead of staying orange.
    HighlightCurrentMonth

End Sub


Private Sub HighlightCurrentMonth()

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:=PASSWORD

    Dim monthNames(11) As String
    monthNames(0) = "Jan": monthNames(1) = "Feb": monthNames(2) = "Mar"
    monthNames(3) = "Apr": monthNames(4) = "May": monthNames(5) = "Jun"
    monthNames(6) = "Jul": monthNames(7) = "Aug": monthNames(8) = "Sep"
    monthNames(9) = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    Dim today As String
    today = monthNames(Month(Now()) - 1) & "-" & Right(CStr(Year(Now())), 2)

    Dim isSaved As Boolean
    isSaved = (GetCell(CELL_IS_SAVED) = "1")

    Dim col As Long
    Dim r As Long
    Dim currentMonthFound As Boolean
    currentMonthFound = False

    For col = ALLOC_FIRST_COL To ALLOC_FIRST_COL + NUM_MONTHS - 1

        Dim hdrVal As Variant
        hdrVal = ws.Cells(HEADER_ROW, col).Value

        Dim isCurrentMonth As Boolean
        isCurrentMonth = False
        If IsDate(hdrVal) Then
            Dim lbl As String
            lbl = monthNames(Month(hdrVal) - 1) & "-" & Right(CStr(Year(hdrVal)), 2)
            isCurrentMonth = (lbl = today)
        End If

        ' --- Header cell colour ---------------------------------------
        If col = ALLOC_FIRST_COL And Not isSaved Then
            ' First column before first save: always orange, regardless of month
            ws.Cells(HEADER_ROW, col).Interior.Color = COLOR_START_UNSAVED
            ws.Cells(HEADER_ROW, col).Font.Color = RGB(0, 0, 0)
        ElseIf isCurrentMonth Then
            ws.Cells(HEADER_ROW, col).Interior.Color = COLOR_HEADER_CURRENT
            ws.Cells(HEADER_ROW, col).Font.Color = RGB(255, 255, 255)
            currentMonthFound = True
        Else
            ws.Cells(HEADER_ROW, col).Interior.Color = COLOR_HEADER_NORMAL
            ws.Cells(HEADER_ROW, col).Font.Color = RGB(0, 0, 0)
        End If

        ' --- Data cell colours ------------------------------------------
        For r = FIRST_DATA_ROW To LAST_DATA_ROW
            If isCurrentMonth Then
                ws.Cells(r, col).Interior.Color = COLOR_DATA_CURRENT
            Else
                ws.Cells(r, col).Interior.Color = COLOR_DATA_NORMAL
            End If
        Next r

    Next col

    ws.Protect Password:=PASSWORD, Contents:=True, _
        DrawingObjects:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


Private Function PushToAPI() As Boolean

    PushToAPI = False
    Dim json As String
    json = BuildPushJSON()
    If json = "" Then
        PushToAPI = True
        Exit Function
    End If

    On Error GoTo Fail
    Dim http As Object
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "POST", API_BASE & "/api/push", False
    http.setRequestHeader "Content-Type", "application/json"
    
    http.send json
    If http.Status = 200 Then PushToAPI = True
    Exit Function
Fail:
End Function


Private Function BuildPushJSON() As String

    BuildPushJSON = ""

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ' Validate start date
    Dim startDate As Variant
    startDate = ws.Range(CELL_START_DATE).Value
    If Not IsDate(startDate) Then Exit Function
    Dim startISO As String
    startISO = Format(CDate(startDate), "yyyy-mm") & "-01"

    ' Build allocations array
    Dim allocJSON As String
    allocJSON = ""
    Dim r As Long
    Dim c As Long

    For r = FIRST_DATA_ROW To LAST_DATA_ROW
        Dim horizonID As String
        horizonID = Trim(CStr(ws.Cells(r, COL_HORIZON_ID).Value))
        If horizonID = "" Then GoTo NextRow

        For c = ALLOC_FIRST_COL To ALLOC_FIRST_COL + NUM_MONTHS - 1
            Dim hdrVal As Variant
            hdrVal = ws.Cells(HEADER_ROW, c).Value
            If Not IsDate(hdrVal) Then GoTo NextCol

            Dim days As Double
            days = 0
            If IsNumeric(ws.Cells(r, c).Value) Then
                days = CDbl(ws.Cells(r, c).Value)
            End If

            If days = 0 Then GoTo NextCol
            If allocJSON <> "" Then allocJSON = allocJSON & ","
            allocJSON = allocJSON & "{" & _
                """horizon_person_number"":""" & JsonEscape(horizonID) & """," & _
                """period_start"":""" & Format(CDate(hdrVal), "yyyy-mm-dd") & """," & _
                """days"":" & Format(days, "0.##") & "}"
NextCol:
        Next c
NextRow:
    Next r

    BuildPushJSON = "{" & _
        """file_path"":""" & Replace(ws.Range(CELL_FILE_PATH).Value, "\", "\\") & """," & _
        """project_number"":""" & JsonEscape(GetCell(CELL_PROJECT_NUMBER)) & """," & _
        """task_order_number"":""" & JsonEscape(GetCell(CELL_TASK_ORDER)) & """," & _
        """project_name"":""" & JsonEscape(GetCell(CELL_PROJECT_NAME)) & """," & _
        """task_name"":""" & JsonEscape(GetCell(CELL_TASK_NAME)) & """," & _
        """project_organisation"":""" & JsonEscape(GetCell(CELL_ORGANISATION)) & """," & _
        """project_director"":""" & JsonEscape(GetCell(CELL_DIRECTOR)) & """," & _
        """project_manager"":""" & JsonEscape(GetCell(CELL_MANAGER)) & """," & _
        """last_updated_by"":""" & JsonEscape(Application.UserName) & """," & _
        """ctc_start_date"":""" & startISO & """," & _
        """allocations"":[" & allocJSON & "]}"

End Function


' =============================================================================
' HTTP
' =============================================================================

Private Function HttpGet(url As String) As String
    HttpGet = ""
    On Error GoTo ErrExit
    Dim http As Object
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "GET", url, False
    http.setRequestHeader "Accept", "application/json"
    http.send
    If http.Status = 200 Then HttpGet = http.responseText
ErrExit:
End Function


' =============================================================================
' JSON HELPERS
' =============================================================================

Private Function WriteStaffData(wsData As Worksheet, json As String) As Long

    Dim fields(3) As String
    fields(0) = "horizon_person_number"
    fields(1) = "name"
    fields(2) = "job_title"
    fields(3) = "job_function"

    Dim rowNum As Long
    rowNum = 0

    Dim pos As Long
    pos = InStr(json, "{")

    Do While pos > 0 And rowNum < 300
        Dim objEnd As Long
        objEnd = InStr(pos, json, "}")
        If objEnd = 0 Then Exit Do

        Dim obj As String
        obj = Mid(json, pos, objEnd - pos + 1)

        Dim f As Long
        For f = 0 To 3
            Dim s As String
            Dim fStart As Long
            Dim fEnd As Long
            s = """" & fields(f) & """: """
            fStart = InStr(obj, s)
            If fStart > 0 Then
                fStart = fStart + Len(s)
                fEnd = InStr(fStart, obj, """")
                If fEnd > 0 Then
                    wsData.Cells(rowNum + 2, f + 1).Value = _
                        Mid(obj, fStart, fEnd - fStart)
                End If
            End If
        Next f

        rowNum = rowNum + 1
        pos = InStr(objEnd, json, "{")
    Loop

    WriteStaffData = rowNum

End Function


Private Function ParseJsonField(json As String, fieldName As String) As String
    ParseJsonField = ""
    If json = "" Then Exit Function
    Dim search As String
    search = """" & fieldName & """: """
    Dim pos As Long
    pos = InStr(json, search)
    If pos = 0 Then Exit Function
    Dim vStart As Long
    Dim vEnd As Long
    vStart = pos + Len(search)
    vEnd = InStr(vStart, json, """")
    If vEnd = 0 Then Exit Function
    ParseJsonField = Mid(json, vStart, vEnd - vStart)
End Function


Private Function IsPlaceholder(s As String) As Boolean
    Select Case LCase(Trim(s))
        Case "", "tbc", "tbd", "n/a", "na"
            IsPlaceholder = True
        Case Else
            IsPlaceholder = False
    End Select
End Function


Private Function JsonEscape(s As String) As String
    Dim r As String
    r = s
    r = Replace(r, "\", "\\")
    r = Replace(r, """", "\""")
    r = Replace(r, Chr(10), "\n")
    r = Replace(r, Chr(13), "\r")
    r = Replace(r, Chr(9), "\t")
    JsonEscape = r
End Function


Private Function URLEncode(s As String) As String
    Dim result As String
    Dim i As Long
    Dim c As String
    result = ""
    For i = 1 To Len(s)
        c = Mid(s, i, 1)
        Select Case c
            Case "A" To "Z", "a" To "z", "0" To "9", "-", "_", ".", "~"
                result = result & c
            Case " "
                result = result & "+"
            Case Else
                result = result & "%" & Hex(Asc(c))
        End Select
    Next i
    URLEncode = result
End Function


Private Sub SetCell(address As String, val As Variant)
    ThisWorkbook.Sheets("Resources").Range(address).Value = val
End Sub


Private Function GetCell(address As String) As String
    On Error Resume Next
    GetCell = CStr(ThisWorkbook.Sheets("Resources").Range(address).Value)
    If Err.Number <> 0 Then GetCell = ""
    On Error GoTo 0
End Function

    
