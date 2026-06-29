Private Sub Workbook_Open()
    CTC_Macro.OnOpen
End Sub

Private Sub Workbook_BeforeSave(ByVal SaveAsUI As Boolean, Cancel As Boolean)
    CTC_Macro.OnBeforeSave Cancel
End Sub

Private Sub Workbook_AfterSave(ByVal Success As Boolean)
    CTC_Macro.OnAfterSave Success
End Sub

Private Sub Workbook_SheetChange(ByVal Sh As Object, ByVal Target As Range)
    If Sh.Name = "Resources" Then
        CTC_Macro.OnNameSelected Sh, Target
    End If
End Sub
