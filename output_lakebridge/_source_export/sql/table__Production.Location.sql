CREATE TABLE [Production].[Location] (
    [LocationID] smallint NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [CostRate] smallmoney NOT NULL,
    [Availability] decimal(8,2) NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
