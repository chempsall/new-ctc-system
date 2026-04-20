Option Explicit

' =============================================================================
' CTC_Macro.bas
' Resource Forecast - CTC Template Macro
' Excel 365
' =============================================================================

' ---------------------------------------------------------------------------
' CONFIGURATION
' ---------------------------------------------------------------------------
Private Const API_BASE As String = "http://localhost:5000"

' Cell addresses in the Resources sheet
Private Const CELL_OFFICE          As String = "C6"
Private Const CELL_START_DATE      As String = "F6"
Private Const CELL_PROJECT_NUMBER  As String = "C7"
Private Const CELL_TASK_ORDER      As String = "F7"
Private Const CELL_PROJECT_NAME    As String = "C8"
Private Const CELL_TASK_NAME       As String = "F8"
Private Const CELL_ORGANISATION    As String = "C9"
Private Const CELL_TEAM            As String = "F9"
Private Const CELL_DIRECTOR        As String = "C10"
Private Const CELL_MANAGER         As String = "F10"
Private Const CELL_HORIZON_STATUS  As String = "C11"
Private Const CELL_LAST_SAVED      As String = "F11"
Private Const CELL_LAST_UPDATED_BY As String = "C12"
Private Const CELL_FILE_PATH       As String = "F12"

' Allocation grid layout
Private Const ALLOC_FIRST_ROW  As Long = 16
Private Const ALLOC_LAST_ROW   As Long = 55
Private Const COL_HORIZON_ID   As Long = 8   ' H
Private Const COL_NAME         As Long = 9   ' I
Private Const COL_GRADE        As Long = 10  ' J
Private Const COL_TEAM         As Long = 11  ' K
Private Const COL_DISCIPLINE   As Long = 12  ' L
Private Const ALLOC_FIRST_COL  As Long = 13  ' M - first month column
Private Const MONTH_HEADER_ROW As Long = 14
Private Const WORKING_DAYS_ROW As Long = 15


' =============================================================================
' SETUP
' Run once after creating a new CTC file from the template.
' Creates named ranges so cells can be referenced by name.
' =============================================================================

Public Sub SetupNamedRanges()

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    With ThisWorkbook.names
        .Add "CTC_Office", ws.Range(CELL_OFFICE)
        .Add "CTCStartDate", ws.Range(CELL_START_DATE)
        .Add "CTC_ProjectNumber", ws.Range(CELL_PROJECT_NUMBER)
        .Add "CTC_TaskOrder", ws.Range(CELL_TASK_ORDER)
        .Add "CTC_ProjectName", ws.Range(CELL_PROJECT_NAME)
        .Add "CTC_TaskName", ws.Range(CELL_TASK_NAME)
        .Add "CTC_Organisation", ws.Range(CELL_ORGANISATION)
        .Add "CTC_Team", ws.Range(CELL_TEAM)
        .Add "CTC_Director", ws.Range(CELL_DIRECTOR)
        .Add "CTC_Manager", ws.Range(CELL_MANAGER)
        .Add "CTC_HorizonStatus", ws.Range(CELL_HORIZON_STATUS)
        .Add "CTC_LastSaved", ws.Range(CELL_LAST_SAVED)
        .Add "CTC_LastUpdatedBy", ws.Range(CELL_LAST_UPDATED_BY)
        .Add "CTC_FilePath", ws.Range(CELL_FILE_PATH)
    End With

    MsgBox "Setup complete. Named ranges created.", vbInformation, "Resource Forecast"

End Sub


' =============================================================================
' ON OPEN
' Called from ThisWorkbook.Workbook_Open
' =============================================================================

Public Sub OnOpen()

    SetCell CELL_FILE_PATH, ThisWorkbook.FullName
    SetCell CELL_LAST_UPDATED_BY, Environ$("USERNAME")

    PopulateOfficeDropdown
    PopulateTeamDropdown
    PopulateStaffDropdown
    PopulateMonthHeaders
    HighlightCurrentMonth

    Application.StatusBar = False

End Sub


' =============================================================================
' BEFORE SAVE
' Called from ThisWorkbook.Workbook_BeforeSave
' =============================================================================

Public Sub OnBeforeSave(Cancel As Boolean)

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ' Validate office
    Dim officeVal As String
    officeVal = Trim$(GetCell(CELL_OFFICE))
    If officeVal = "" Then
        MsgBox "Please select an office before saving.", vbExclamation, "Save blocked"
        ws.Range(CELL_OFFICE).Select
        Cancel = True
        Exit Sub
    End If

    ' Validate CTC start date
    Dim startDateVal As String
    startDateVal = MonthYearLabelFromValue(ws.Range(CELL_START_DATE).value)

    If startDateVal = "" Then
        MsgBox "Please set the CTC start date before saving." & vbCrLf & _
               "Format: MMM-YYYY  e.g. Apr-2026", vbExclamation, "Save blocked"
        ws.Range(CELL_START_DATE).Select
        Cancel = True
        Exit Sub
    End If

    If Not IsValidMonthYear(startDateVal) Then
        MsgBox "CTC start date format is incorrect." & vbCrLf & _
               "Use MMM-YYYY format, e.g. Apr-2026", vbExclamation, "Invalid date"
        ws.Range(CELL_START_DATE).Select
        Cancel = True
        Exit Sub
    End If

    ' Normalise stored value if it is a real date
    If IsDate(ws.Range(CELL_START_DATE).value) Then
        ws.Range(CELL_START_DATE).value = DateSerial( _
            Year(ws.Range(CELL_START_DATE).value), _
            Month(ws.Range(CELL_START_DATE).value), _
            1)
        ws.Range(CELL_START_DATE).NumberFormat = "mmm-yyyy"
    End If

    ' Warn about placeholder project number (non-blocking)
    Dim projNum As String
    projNum = Trim$(GetCell(CELL_PROJECT_NUMBER))
    If IsPlaceholder(projNum) Then
        MsgBox "Reminder: project number (" & projNum & ") looks like a placeholder." & vbCrLf & _
               "Update it with the Horizon number when available." & vbCrLf & _
               "The file will save normally.", vbInformation, "Project number reminder"
    End If

    ' Update macro-owned fields
    SetCell CELL_LAST_SAVED, Now
    SetCell CELL_LAST_UPDATED_BY, Environ$("USERNAME")
    SetCell CELL_FILE_PATH, ThisWorkbook.FullName

    ' Push to server
    If Not PushToAPI() Then
        MsgBox "Could not connect to the resource forecast server." & vbCrLf & _
               "The file will save locally.", vbExclamation, "Server not reachable"
    End If

End Sub


' =============================================================================
' NAME SELECTED
' Called from ThisWorkbook.Workbook_SheetChange when a name is picked
' =============================================================================

Public Sub OnNameSelected(ws As Worksheet, changedCell As Range)

    If changedCell.Column <> COL_NAME Then Exit Sub
    If changedCell.row < ALLOC_FIRST_ROW Or changedCell.row > ALLOC_LAST_ROW Then Exit Sub

    Dim selectedName As String
    selectedName = Trim$(changedCell.value)

    ' Clear row if name removed
    If selectedName = "" Then
        ws.Cells(changedCell.row, COL_HORIZON_ID).value = ""
        ws.Cells(changedCell.row, COL_GRADE).value = ""
        ws.Cells(changedCell.row, COL_TEAM).value = ""
        ws.Cells(changedCell.row, COL_DISCIPLINE).value = ""
        Exit Sub
    End If

    ' Look up in hidden _StaffData sheet
    Dim wsData As Worksheet
    On Error Resume Next
    Set wsData = ThisWorkbook.Sheets("_StaffData")
    On Error GoTo 0

    If wsData Is Nothing Then
        MsgBox "Staff data not loaded. Please close and reopen the file.", vbExclamation
        Exit Sub
    End If

    Dim lastRow As Long
    Dim i As Long
    lastRow = wsData.Cells(wsData.Rows.count, 2).End(xlUp).row

    For i = 1 To lastRow
        If wsData.Cells(i, 2).value = selectedName Then
            ws.Cells(changedCell.row, COL_HORIZON_ID).value = wsData.Cells(i, 1).value
            ws.Cells(changedCell.row, COL_GRADE).value = wsData.Cells(i, 3).value
            ws.Cells(changedCell.row, COL_TEAM).value = wsData.Cells(i, 4).value
            ws.Cells(changedCell.row, COL_DISCIPLINE).value = wsData.Cells(i, 5).value
            Exit For
        End If
    Next i

End Sub


' =============================================================================
' API
' =============================================================================

Private Function PushToAPI() As Boolean

    PushToAPI = False

    Dim jsonBody As String
    jsonBody = BuildPushJSON()
    If jsonBody = "" Then
        PushToAPI = True
        Exit Function
    End If

    On Error GoTo HttpError
    Dim http As Object
    Set http = CreateObject("MSXML2.XMLHTTP")
    http.Open "POST", API_BASE & "/api/push", False
    http.setRequestHeader "Content-Type", "application/json"
    http.Send jsonBody
    If http.Status = 200 Then PushToAPI = True
    Exit Function

HttpError:
    PushToAPI = False

End Function


Private Function BuildPushJSON() As String

    BuildPushJSON = ""

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    Dim startISO As String
    startISO = MonthYearToISO(MonthYearLabelFromValue(ws.Range(CELL_START_DATE).value))
    If startISO = "" Then Exit Function

    ' Build allocations array
    Dim allocJSON As String
    allocJSON = ""
    Dim r As Long
    Dim c As Long

    For r = ALLOC_FIRST_ROW To ALLOC_LAST_ROW
        Dim horizonID As String
        horizonID = Trim$(CStr(ws.Cells(r, COL_HORIZON_ID).value))
        If horizonID = "" Then GoTo SkipRow

        For c = ALLOC_FIRST_COL To ALLOC_FIRST_COL + 35
            Dim label As String
            label = Trim$(CStr(ws.Cells(MONTH_HEADER_ROW, c).value))
            If label = "" Or Left$(label, 1) = "[" Then GoTo SkipCol

            Dim periodISO As String
            periodISO = MonthYearToISO(label)
            If periodISO = "" Then GoTo SkipCol

            Dim days As Double
            days = 0
            If IsNumeric(ws.Cells(r, c).value) Then days = CDbl(ws.Cells(r, c).value)

            If allocJSON <> "" Then allocJSON = allocJSON & ","
            allocJSON = allocJSON & "{" & _
                """horizon_person_number"":""" & JsonEscape(horizonID) & """," & _
                """period_start"":""" & periodISO & """," & _
                """days"":" & Format$(days, "0.##") & "}"
SkipCol:
        Next c
SkipRow:
    Next r

    BuildPushJSON = "{" & _
        """file_path"":""" & JsonEscape(ThisWorkbook.FullName) & """," & _
        """office"":""" & JsonEscape(GetCell(CELL_OFFICE)) & """," & _
        """project_number"":""" & JsonEscape(GetCell(CELL_PROJECT_NUMBER)) & """," & _
        """task_order_number"":""" & JsonEscape(GetCell(CELL_TASK_ORDER)) & """," & _
        """project_name"":""" & JsonEscape(GetCell(CELL_PROJECT_NAME)) & """," & _
        """task_name"":""" & JsonEscape(GetCell(CELL_TASK_NAME)) & """," & _
        """project_organisation"":""" & JsonEscape(GetCell(CELL_ORGANISATION)) & """," & _
        """staff_team"":""" & JsonEscape(GetCell(CELL_TEAM)) & """," & _
        """project_director"":""" & JsonEscape(GetCell(CELL_DIRECTOR)) & """," & _
        """project_manager"":""" & JsonEscape(GetCell(CELL_MANAGER)) & """," & _
        """last_updated_by"":""" & JsonEscape(Environ$("USERNAME")) & """," & _
        """ctc_start_date"":""" & startISO & """," & _
        """allocations"":[" & allocJSON & "]}"

End Function


' =============================================================================
' DROPDOWN POPULATION
' =============================================================================

Private Sub PopulateOfficeDropdown()

    Dim data As String
    data = HttpGet(API_BASE & "/api/offices")
    If data = "" Then Exit Sub

    Dim items() As String
    items = ParseStringArray(data, "office_name")
    If Not IsArrayAllocated(items) Then Exit Sub

    With ThisWorkbook.Sheets("Resources").Range(CELL_OFFICE).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="""" & Join(items, ",") & """"
    End With

End Sub


Private Sub PopulateTeamDropdown()

    Dim officeVal As String
    officeVal = Trim$(GetCell(CELL_OFFICE))

    Dim url As String
    If officeVal <> "" Then
        url = API_BASE & "/api/teams?office=" & URLEncode(officeVal)
    Else
        url = API_BASE & "/api/teams"
    End If

    Dim data As String
    data = HttpGet(url)
    If data = "" Then Exit Sub

    Dim items() As String
    items = ParseStringArray(data, "team_name")
    If Not IsArrayAllocated(items) Then Exit Sub

    With ThisWorkbook.Sheets("Resources").Range(CELL_TEAM).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="""" & Join(items, ",") & """"
    End With

End Sub


Private Sub PopulateStaffDropdown()

    Dim officeVal As String
    officeVal = Trim$(GetCell(CELL_OFFICE))

    Dim url As String
    If officeVal <> "" Then
        url = API_BASE & "/api/staff?office=" & URLEncode(officeVal)
    Else
        url = API_BASE & "/api/staff"
    End If

    Dim data As String
    data = HttpGet(url)
    If data = "" Then Exit Sub

    ' Create or clear hidden _StaffData sheet
    Dim wsData As Worksheet
    On Error Resume Next
    Set wsData = ThisWorkbook.Sheets("_StaffData")
    On Error GoTo 0

    If wsData Is Nothing Then
        Set wsData = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.count))
        wsData.Name = "_StaffData"
    Else
        wsData.Cells.Clear
    End If
    wsData.Visible = xlSheetVeryHidden

    Dim rowCount As Long
    rowCount = WriteStaffData(wsData, data)
    If rowCount = 0 Then Exit Sub

    ' Apply dropdown to name column
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    With ws.Range(ws.Cells(ALLOC_FIRST_ROW, COL_NAME), _
                  ws.Cells(ALLOC_LAST_ROW, COL_NAME)).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="=_StaffData!$B$1:$B$" & rowCount
    End With

End Sub


Private Sub PopulateMonthHeaders()

    Dim startVal As String
    startVal = MonthYearLabelFromValue(ThisWorkbook.Sheets("Resources").Range(CELL_START_DATE).value)
    If Not IsValidMonthYear(startVal) Then Exit Sub

    Dim startISO As String
    startISO = MonthYearToISO(startVal)
    If startISO = "" Then Exit Sub

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    Dim yr As Long
    Dim mo As Long
    yr = CLng(Left$(startISO, 4))
    mo = CLng(Mid$(startISO, 6, 2))

    Dim monthNames(11) As String
    monthNames(0) = "Jan": monthNames(1) = "Feb": monthNames(2) = "Mar"
    monthNames(3) = "Apr": monthNames(4) = "May": monthNames(5) = "Jun"
    monthNames(6) = "Jul": monthNames(7) = "Aug": monthNames(8) = "Sep"
    monthNames(9) = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    Dim summaryData As String
    summaryData = HttpGet(API_BASE & "/api/summary")

    Dim col As Long
    Dim m As Long
    For m = 0 To 35
        col = ALLOC_FIRST_COL + m

        Dim label As String
        label = monthNames(mo - 1) & "-" & CStr(yr)

        With ws.Cells(MONTH_HEADER_ROW, col)
            .value = label
            .Font.Bold = True
            .Font.Color = RGB(255, 255, 255)
            .Interior.Color = RGB(46, 94, 170)
            .HorizontalAlignment = xlCenter
        End With

        If summaryData <> "" Then
            Dim wdSearch As String
            Dim wdPos As Long
            wdSearch = """" & label & """:"
            wdPos = InStr(summaryData, wdSearch)
            If wdPos > 0 Then
                wdPos = wdPos + Len(wdSearch) + 1
                Dim wdStr As String
                wdStr = ""
                Do While Mid$(summaryData, wdPos, 1) Like "[0-9]"
                    wdStr = wdStr & Mid$(summaryData, wdPos, 1)
                    wdPos = wdPos + 1
                Loop
                If wdStr <> "" Then ws.Cells(WORKING_DAYS_ROW, col).value = CLng(wdStr)
            End If
        End If

        mo = mo + 1
        If mo > 12 Then
            mo = 1
            yr = yr + 1
        End If
    Next m

End Sub


Private Sub HighlightCurrentMonth()

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    Dim monthNames(11) As String
    monthNames(0) = "Jan": monthNames(1) = "Feb": monthNames(2) = "Mar"
    monthNames(3) = "Apr": monthNames(4) = "May": monthNames(5) = "Jun"
    monthNames(6) = "Jul": monthNames(7) = "Aug": monthNames(8) = "Sep"
    monthNames(9) = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    Dim today As String
    today = monthNames(Month(Now) - 1) & "-" & Year(Now)

    Dim col As Long
    Dim r As Long
    For col = ALLOC_FIRST_COL To ALLOC_FIRST_COL + 35
        If Trim$(CStr(ws.Cells(MONTH_HEADER_ROW, col).value)) = today Then
            ws.Cells(MONTH_HEADER_ROW, col).Interior.Color = RGB(31, 56, 100)
            For r = ALLOC_FIRST_ROW To ALLOC_LAST_ROW
                ws.Cells(r, col).Interior.Color = RGB(235, 243, 255)
            Next r
            Exit For
        End If
    Next col

End Sub


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
    http.Send
    If http.Status = 200 Then HttpGet = http.responseText
ErrExit:
End Function


' =============================================================================
' JSON / STRING HELPERS
' =============================================================================

Private Function ParseStringArray(json As String, fieldName As String) As String()

    Dim results() As String
    ReDim results(0 To 200)
    Dim count As Long
    count = 0

    Dim search As String
    search = """" & fieldName & """:"""

    Dim pos As Long
    pos = 1

    Do While count < 200
        pos = InStr(pos, json, search)
        If pos = 0 Then Exit Do
        Dim vStart As Long
        Dim vEnd As Long
        vStart = pos + Len(search)
        vEnd = InStr(vStart, json, """")
        If vEnd = 0 Then Exit Do
        results(count) = Mid$(json, vStart, vEnd - vStart)
        count = count + 1
        pos = vEnd + 1
    Loop

    If count = 0 Then Exit Function
    ReDim Preserve results(0 To count - 1)
    ParseStringArray = results

End Function


Private Function WriteStaffData(wsData As Worksheet, json As String) As Long

    Dim fields(4) As String
    fields(0) = "horizon_person_number"
    fields(1) = "name"
    fields(2) = "technical_grade"
    fields(3) = "staff_team"
    fields(4) = "discipline"

    Dim rowNum As Long
    rowNum = 0

    Dim pos As Long
    pos = InStr(json, "{")

    Do While pos > 0 And rowNum < 200
        Dim objEnd As Long
        objEnd = InStr(pos, json, "}")
        If objEnd = 0 Then Exit Do

        Dim obj As String
        obj = Mid$(json, pos, objEnd - pos + 1)

        Dim f As Long
        For f = 0 To 4
            Dim s As String
            Dim fStart As Long
            Dim fEnd As Long
            s = """" & fields(f) & """:"""
            fStart = InStr(obj, s)
            If fStart > 0 Then
                fStart = fStart + Len(s)
                fEnd = InStr(fStart, obj, """")
                If fEnd > 0 Then
                    wsData.Cells(rowNum + 1, f + 1).value = Mid$(obj, fStart, fEnd - fStart)
                End If
            End If
        Next f

        rowNum = rowNum + 1
        pos = InStr(objEnd, json, "{")
    Loop

    WriteStaffData = rowNum

End Function


Private Function IsArrayAllocated(arr() As String) As Boolean
    On Error Resume Next
    IsArrayAllocated = (UBound(arr) >= 0)
    If Err.Number <> 0 Then IsArrayAllocated = False
    On Error GoTo 0
End Function


Private Function IsValidMonthYear(s As String) As Boolean
    IsValidMonthYear = False

    s = Trim$(s)
    If Len(s) <> 8 Then Exit Function
    If Mid$(s, 4, 1) <> "-" Then Exit Function

    Dim mon As String
    mon = StrConv(Left$(s, 3), vbProperCase)

    If InStr(1, "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", mon, vbTextCompare) = 0 Then Exit Function
    If Not IsNumeric(Right$(s, 4)) Then Exit Function

    IsValidMonthYear = True
End Function


Private Function MonthYearLabelFromValue(v As Variant) As String

    Dim monthNames(11) As String
    monthNames(0) = "Jan": monthNames(1) = "Feb": monthNames(2) = "Mar"
    monthNames(3) = "Apr": monthNames(4) = "May": monthNames(5) = "Jun"
    monthNames(6) = "Jul": monthNames(7) = "Aug": monthNames(8) = "Sep"
    monthNames(9) = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    If IsDate(v) Then
        MonthYearLabelFromValue = monthNames(Month(CDate(v)) - 1) & "-" & CStr(Year(CDate(v)))
        Exit Function
    End If

    MonthYearLabelFromValue = Trim$(CStr(v))

End Function


Private Function MonthYearToISO(s As String) As String
    MonthYearToISO = ""
    If Not IsValidMonthYear(s) Then Exit Function

    Dim monthNames(11) As String
    monthNames(0) = "Jan": monthNames(1) = "Feb": monthNames(2) = "Mar"
    monthNames(3) = "Apr": monthNames(4) = "May": monthNames(5) = "Jun"
    monthNames(6) = "Jul": monthNames(7) = "Aug": monthNames(8) = "Sep"
    monthNames(9) = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    Dim m As Long
    For m = 0 To 11
        If monthNames(m) = StrConv(Left$(s, 3), vbProperCase) Then
            MonthYearToISO = Right$(s, 4) & "-" & Format$(m + 1, "00") & "-01"
            Exit Function
        End If
    Next m
End Function


Private Function IsPlaceholder(s As String) As Boolean
    Dim c As String
    c = LCase$(Trim$(s))
    Select Case c
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
    r = Replace(r, Chr$(10), "\n")
    r = Replace(r, Chr$(13), "\r")
    r = Replace(r, Chr$(9), "\t")
    JsonEscape = r
End Function


Private Function URLEncode(s As String) As String
    Dim result As String
    Dim i As Long
    Dim c As String
    result = ""
    For i = 1 To Len(s)
        c = Mid$(s, i, 1)
        Select Case c
            Case "A" To "Z", "a" To "z", "0" To "9", "-", "_", ".", "~"
                result = result & c
            Case " "
                result = result & "+"
            Case Else
                result = result & "%" & Hex$(Asc(c))
        End Select
    Next i
    URLEncode = result
End Function


Private Sub SetCell(address As String, val As Variant)
    ThisWorkbook.Sheets("Resources").Range(address).value = val
End Sub


Private Function GetCell(address As String) As String
    On Error Resume Next
    GetCell = CStr(ThisWorkbook.Sheets("Resources").Range(address).value)
    If Err.Number <> 0 Then GetCell = ""
    On Error GoTo 0
End Function