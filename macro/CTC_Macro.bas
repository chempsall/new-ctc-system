Attribute VB_Name = "CTC_Macro"
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

' Cell addresses (named ranges preferred, direct refs as fallback)
Private Const CELL_START_DATE      As String = "E1"
Private Const CELL_PROJECT_NUMBER  As String = "B3"
Private Const CELL_TASK_ORDER      As String = "B4"
Private Const CELL_OFFICE          As String = "B5"
Private Const CELL_ORGANISATION    As String = "B6"
Private Const CELL_CUSTOMER        As String = "B7"
Private Const CELL_PROJECT_NAME    As String = "B8"
Private Const CELL_TASK_NAME       As String = "B9"
Private Const CELL_DIRECTOR        As String = "B10"
Private Const CELL_MANAGER         As String = "B11"
Private Const CELL_LAST_UPDATED_BY As String = "B12"
Private Const CELL_FILE_PATH       As String = "A13"   ' Hidden

' Allocation grid
Private Const HEADER_ROW       As Long = 16   ' Column header row
Private Const WORKING_DAYS_ROW As Long = 14
Private Const FIRST_DATA_ROW   As Long = 17
Private Const LAST_DATA_ROW    As Long = 56
Private Const COL_HORIZON_ID   As Long = 1    ' A (hidden)
Private Const COL_NAME         As Long = 2    ' B
Private Const COL_GRADE        As Long = 3    ' C
Private Const COL_TEAM         As Long = 4    ' D
Private Const COL_DISCIPLINE   As Long = 5    ' E
Private Const ALLOC_FIRST_COL  As Long = 6    ' F = first month


' =============================================================================
' ON OPEN
' =============================================================================

Public Sub OnOpen()

    SetCell CELL_FILE_PATH,       ThisWorkbook.FullName
    SetCell CELL_LAST_UPDATED_BY, Application.UserName

    ' Populate dropdowns from API
    PopulateOfficeDropdown
    PopulateTeamDropdown
    PopulateStaffDropdown

    ' Highlight current month column
    HighlightCurrentMonth

    Application.StatusBar = False

End Sub


' =============================================================================
' BEFORE SAVE
' =============================================================================

Public Sub OnBeforeSave(Cancel As Boolean)

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ' --- Validate required fields -------------------------------------------

    Dim officeVal As String
    officeVal = Trim(GetCell(CELL_OFFICE))
    If officeVal = "" Then
        MsgBox "Please select an Office before saving.", vbExclamation, "Save blocked"
        ws.Range(CELL_OFFICE).Select
        Cancel = True
        Exit Sub
    End If

    Dim startDateVal As Variant
    startDateVal = ws.Range(CELL_START_DATE).Value
    If IsEmpty(startDateVal) Or startDateVal = "" Then
        MsgBox "Please set the CTC Start Date before saving." & vbCrLf & _
               "Enter as a date in the yellow cell (top right).", _
               vbExclamation, "Save blocked"
        ws.Range(CELL_START_DATE).Select
        Cancel = True
        Exit Sub
    End If

    ' --- Warn about placeholder project number (non-blocking) ---------------

    Dim projNum As String
    projNum = Trim(GetCell(CELL_PROJECT_NUMBER))
    If IsPlaceholder(projNum) Then
        MsgBox "Reminder: project number (" & projNum & ") looks like a placeholder." & vbCrLf & _
               "Update it with the Horizon number when it is available." & vbCrLf & _
               "The file will save normally.", vbInformation, "Project number reminder"
    End If

    ' --- Lookup Horizon record if not already populated ---------------------
    ' Only triggers if project name is still showing default value
    If GetCell(CELL_PROJECT_NAME) = "No Horizon Record Found" _
       Or GetCell(CELL_PROJECT_NAME) = "" Then
        If projNum <> "" And Not IsPlaceholder(projNum) Then
            LookupProjectFromAPI projNum, Trim(GetCell(CELL_TASK_ORDER))
        End If
    End If

    ' --- Update macro-owned fields ------------------------------------------

    ' Unprotect to write macro fields
    ws.Unprotect Password:="Cyberdyne"

    SetCell CELL_LAST_UPDATED_BY, Application.UserName
    SetCell CELL_FILE_PATH,       ThisWorkbook.FullName

    ws.Protect Password:="Cyberdyne", _
        DrawingObjects:=True, Contents:=True, Scenarios:=True, _
        AllowFormattingCells:=False, AllowFormattingColumns:=False, _
        AllowFormattingRows:=False, AllowInsertingColumns:=False, _
        AllowInsertingRows:=False, AllowDeletingColumns:=False, _
        AllowDeletingRows:=False, AllowSorting:=False, _
        AllowFiltering:=False, UserInterfaceOnly:=True

    ' --- Push to server -----------------------------------------------------

    If Not PushToAPI() Then
        MsgBox "Could not connect to the resource forecast server." & vbCrLf & _
               "The file has been saved locally." & vbCrLf & _
               "Data will sync on the next successful save.", _
               vbExclamation, "Server not reachable"
    End If

End Sub


' =============================================================================
' NAME SELECTED — fires when a name is picked in the allocation grid
' =============================================================================

Public Sub OnNameSelected(ws As Worksheet, changedCell As Range)

    If changedCell.Column <> COL_NAME Then Exit Sub
    If changedCell.Row <= HEADER_ROW Then Exit Sub
    If changedCell.Row > LAST_DATA_ROW Then Exit Sub

    Dim selectedName As String
    selectedName = Trim(changedCell.Value)

    ws.Unprotect Password:="Cyberdyne"

    If selectedName = "" Then
        ws.Cells(changedCell.Row, COL_HORIZON_ID).Value = ""
        ws.Cells(changedCell.Row, COL_GRADE).Value      = ""
        ws.Cells(changedCell.Row, COL_TEAM).Value       = ""
        ws.Cells(changedCell.Row, COL_DISCIPLINE).Value = ""
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
    For i = 2 To lastRow  ' Row 1 is headers
        If wsData.Cells(i, 2).Value = selectedName Then
            ws.Cells(changedCell.Row, COL_HORIZON_ID).Value = wsData.Cells(i, 1).Value
            ws.Cells(changedCell.Row, COL_GRADE).Value      = wsData.Cells(i, 3).Value
            ws.Cells(changedCell.Row, COL_TEAM).Value       = wsData.Cells(i, 4).Value
            ws.Cells(changedCell.Row, COL_DISCIPLINE).Value = wsData.Cells(i, 5).Value
            Exit For
        End If
    Next i

Reprotect:
    ws.Protect Password:="Cyberdyne", _
        DrawingObjects:=True, Contents:=True, Scenarios:=True, _
        UserInterfaceOnly:=True

End Sub


' =============================================================================
' PROJECT LOOKUP
' =============================================================================

Private Sub LookupProjectFromAPI(projNum As String, taskOrder As String)

    If projNum = "" Or taskOrder = "" Then Exit Sub

    Dim url As String
    url = API_BASE & "/api/project?project_number=" & URLEncode(projNum) & _
          "&task_order_number=" & URLEncode(taskOrder)

    Dim data As String
    data = HttpGet(url)
    If data = "" Then Exit Sub

    ' Parse JSON response and populate blue cells
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ws.Unprotect Password:="Cyberdyne"

    Dim projName As String
    Dim taskName As String
    Dim org      As String
    Dim customer As String
    Dim director As String
    Dim manager  As String

    projName = ParseJsonField(data, "project_name")
    taskName = ParseJsonField(data, "task_name")
    org      = ParseJsonField(data, "project_organisation")
    customer = ParseJsonField(data, "project_customer")
    director = ParseJsonField(data, "project_director")
    manager  = ParseJsonField(data, "project_manager")

    If projName <> "" Then SetCell CELL_PROJECT_NAME, projName
    If taskName <> "" Then SetCell CELL_TASK_NAME,    taskName
    If org      <> "" Then SetCell CELL_ORGANISATION, org
    If customer <> "" Then SetCell CELL_CUSTOMER,     customer
    If director <> "" Then SetCell CELL_DIRECTOR,     director
    If manager  <> "" Then SetCell CELL_MANAGER,      manager

    ws.Protect Password:="Cyberdyne", _
        DrawingObjects:=True, Contents:=True, Scenarios:=True, _
        UserInterfaceOnly:=True

End Sub


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

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:="Cyberdyne"

    With ws.Range(CELL_OFFICE).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="""" & Join(items, ",") & """"
        .ShowError = False
    End With

    ws.Protect Password:="Cyberdyne", DrawingObjects:=True, _
        Contents:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


Private Sub PopulateTeamDropdown()

    Dim url As String
    Dim officeVal As String
    officeVal = Trim(GetCell(CELL_OFFICE))

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

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:="Cyberdyne"

    ' Team dropdown applies to the header office/team row AND each staff row team col
    With ws.Range(CELL_OFFICE).Offset(0, 0).Validation  ' placeholder - team is in staff rows
        ' Team in header
    End With

    ws.Protect Password:="Cyberdyne", DrawingObjects:=True, _
        Contents:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


Private Sub PopulateStaffDropdown()

    Dim officeVal As String
    officeVal = Trim(GetCell(CELL_OFFICE))

    Dim url As String
    If officeVal <> "" Then
        url = API_BASE & "/api/staff?office=" & URLEncode(officeVal)
    Else
        url = API_BASE & "/api/staff"
    End If

    Dim data As String
    data = HttpGet(url)
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
        wsData.Visible = xlSheetVeryHidden
    Else
        wsData.Cells.Clear
    End If

    ' Headers
    wsData.Cells(1, 1).Value = "Horizon ID"
    wsData.Cells(1, 2).Value = "Name"
    wsData.Cells(1, 3).Value = "Grade"
    wsData.Cells(1, 4).Value = "Team"
    wsData.Cells(1, 5).Value = "Discipline"

    Dim rowCount As Long
    rowCount = WriteStaffData(wsData, data)
    If rowCount = 0 Then Exit Sub

    ' Apply name dropdown to staff grid
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:="Cyberdyne"

    With ws.Range(ws.Cells(FIRST_DATA_ROW, COL_NAME), _
                  ws.Cells(LAST_DATA_ROW, COL_NAME)).Validation
        .Delete
        .Add Type:=xlValidateList, AlertStyle:=xlValidAlertStop, _
             Formula1:="=_StaffData!$B$2:$B$" & (rowCount + 1)
        .ShowError = False
    End With

    ws.Protect Password:="Cyberdyne", DrawingObjects:=True, _
        Contents:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


Private Sub HighlightCurrentMonth()

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")
    ws.Unprotect Password:="Cyberdyne"

    Dim monthNames(11) As String
    monthNames(0)  = "Jan": monthNames(1)  = "Feb": monthNames(2)  = "Mar"
    monthNames(3)  = "Apr": monthNames(4)  = "May": monthNames(5)  = "Jun"
    monthNames(6)  = "Jul": monthNames(7)  = "Aug": monthNames(8)  = "Sep"
    monthNames(9)  = "Oct": monthNames(10) = "Nov": monthNames(11) = "Dec"

    Dim today As String
    today = monthNames(Month(Now()) - 1) & "-" & Right(CStr(Year(Now())), 2)

    Dim col As Long
    Dim r As Long
    For col = ALLOC_FIRST_COL To ALLOC_FIRST_COL + 35
        Dim hdr As String
        Dim hdrDate As Variant
        hdrDate = ws.Cells(15, col).Value
        If IsDate(hdrDate) Then
            hdr = monthNames(Month(hdrDate) - 1) & "-" & Right(CStr(Year(hdrDate)), 2)
            If hdr = today Then
                ws.Cells(15, col).Interior.Color = RGB(23, 55, 94)   ' darker blue
                For r = FIRST_DATA_ROW To LAST_DATA_ROW
                    ws.Cells(r, col).Interior.Color = RGB(220, 230, 241)
                Next r
                Exit For
            End If
        End If
    Next col

    ws.Protect Password:="Cyberdyne", DrawingObjects:=True, _
        Contents:=True, Scenarios:=True, UserInterfaceOnly:=True

End Sub


' =============================================================================
' API PUSH
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
    http.send jsonBody
    If http.Status = 200 Then PushToAPI = True
    Exit Function

HttpError:
    PushToAPI = False

End Function


Private Function BuildPushJSON() As String

    BuildPushJSON = ""

    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Resources")

    ' CTC start date from named range
    Dim startDate As Variant
    startDate = ws.Range("CTCStartDate").Value
    If Not IsDate(startDate) Then Exit Function

    Dim startISO As String
    startISO = Format(CDate(startDate), "yyyy-mm") & "-01"

    ' Build allocations
    Dim allocJSON As String
    allocJSON = ""
    Dim r As Long
    Dim c As Long

    For r = FIRST_DATA_ROW To LAST_DATA_ROW
        Dim horizonID As String
        horizonID = Trim(CStr(ws.Cells(r, COL_HORIZON_ID).Value))
        If horizonID = "" Then GoTo SkipRow

        For c = ALLOC_FIRST_COL To ALLOC_FIRST_COL + 35
            Dim hdrDate As Variant
            hdrDate = ws.Cells(15, c).Value
            If Not IsDate(hdrDate) Then GoTo SkipCol

            Dim periodISO As String
            periodISO = Format(CDate(hdrDate), "yyyy-mm-dd")

            Dim days As Double
            days = 0
            If IsNumeric(ws.Cells(r, c).Value) Then
                days = CDbl(ws.Cells(r, c).Value)
            End If

            If allocJSON <> "" Then allocJSON = allocJSON & ","
            allocJSON = allocJSON & "{" & _
                """horizon_person_number"":""" & JsonEscape(horizonID) & """," & _
                """period_start"":""" & periodISO & """," & _
                """days"":" & Format(days, "0.##") & "}"
SkipCol:
        Next c
SkipRow:
    Next r

    BuildPushJSON = "{" & _
        """file_path"":"""            & JsonEscape(ws.Range("CTC_FilePath").Value)      & """," & _
        """office"":"""               & JsonEscape(GetCell(CELL_OFFICE))                & """," & _
        """project_number"":"""       & JsonEscape(GetCell(CELL_PROJECT_NUMBER))        & """," & _
        """task_order_number"":"""    & JsonEscape(GetCell(CELL_TASK_ORDER))            & """," & _
        """project_name"":"""         & JsonEscape(GetCell(CELL_PROJECT_NAME))          & """," & _
        """task_name"":"""            & JsonEscape(GetCell(CELL_TASK_NAME))             & """," & _
        """project_organisation"":""" & JsonEscape(GetCell(CELL_ORGANISATION))         & """," & _
        """staff_team"":"""           & JsonEscape(GetCell(CELL_OFFICE))                & """," & _
        """project_director"":"""     & JsonEscape(GetCell(CELL_DIRECTOR))              & """," & _
        """project_manager"":"""      & JsonEscape(GetCell(CELL_MANAGER))               & """," & _
        """last_updated_by"":"""      & JsonEscape(Application.UserName)               & """," & _
        """ctc_start_date"":"""       & startISO                                        & """," & _
        """allocations"":["           & allocJSON                                       & "]}"

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
        results(count) = Mid(json, vStart, vEnd - vStart)
        count = count + 1
        pos = vEnd + 1
    Loop

    If count = 0 Then Exit Function
    ReDim Preserve results(0 To count - 1)
    ParseStringArray = results

End Function


Private Function ParseJsonField(json As String, fieldName As String) As String
    ParseJsonField = ""
    Dim search As String
    search = """" & fieldName & """:"""
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
        obj = Mid(json, pos, objEnd - pos + 1)

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


Private Function IsArrayAllocated(arr() As String) As Boolean
    On Error Resume Next
    IsArrayAllocated = (UBound(arr) >= 0)
    If Err.Number <> 0 Then IsArrayAllocated = False
    On Error GoTo 0
End Function


Private Function IsPlaceholder(s As String) As Boolean
    Dim c As String
    c = LCase(Trim(s))
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
