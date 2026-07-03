CREATE TABLE [HumanResources].[Shift] (
    [ShiftID] tinyint NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [StartTime] time NOT NULL,
    [EndTime] time NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
